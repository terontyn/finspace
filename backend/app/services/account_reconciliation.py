import hashlib
import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.account_reconciliation import (
    AccountReconciliation,
    AccountReconciliationItem,
)
from app.db.models.accounts import Account
from app.db.models.transactions import FinancialTransaction
from app.dependencies.context import RequestContext
from app.repositories import account_reconciliation as repository
from app.schemas.account_reconciliation import (
    AccountReconciliationConfirmRequest,
    AccountReconciliationPreview,
    AccountReconciliationPreviewRequest,
    AccountReconciliationResponse,
    AccountReconciliationTransaction,
)
from app.schemas.transactions import TransactionStatus, TransactionType
from app.services.audit import record_audit, snapshot
from app.services.financial_period_guard import get_or_create_control
from app.services.sync_outbox import enqueue_entity

MONEY_STEP = Decimal("0.0001")
EFFECTIVE_STATUSES = ("confirmed", "reconciled")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_STEP)


def _workspace_zone(context: RequestContext) -> ZoneInfo:
    try:
        return ZoneInfo(context.workspace.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ApiError(
            status_code=500,
            code="WORKSPACE_TIMEZONE_INVALID",
            message="Workspace timezone is invalid",
        ) from exc


def _cutoff_at(context: RequestContext, statement_date: date) -> datetime:
    next_day = statement_date + timedelta(days=1)
    return datetime.combine(next_day, time.min, tzinfo=_workspace_zone(context)).astimezone(UTC)


async def _account(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Account:
    statement = select(Account).where(
        Account.id == account_id,
        Account.workspace_id == context.workspace.id,
        Account.deleted_at.is_(None),
    )
    if for_update:
        statement = statement.with_for_update()
    account = await session.scalar(statement)
    if account is None:
        raise ApiError(status_code=404, code="ACCOUNT_NOT_FOUND", message="Account was not found")
    return account


def _validate_request(
    context: RequestContext,
    account: Account,
    data: AccountReconciliationPreviewRequest,
) -> None:
    if data.currency != account.currency:
        raise ApiError(
            status_code=422,
            code="CURRENCY_MISMATCH",
            message="Statement currency must match account currency",
        )
    if data.account_version != account.version:
        raise ApiError(
            status_code=409,
            code="VERSION_CONFLICT",
            message="Account was modified after the reconciliation was opened",
            details={"current_version": account.version},
        )
    opening_date = account.opening_balance_at.astimezone(_workspace_zone(context)).date()
    if data.statement_date < opening_date:
        raise ApiError(
            status_code=422,
            code="STATEMENT_DATE_BEFORE_ACCOUNT_OPENING",
            message="Statement date cannot be before the account opening date",
        )


async def _effective_transactions(
    session: AsyncSession,
    context: RequestContext,
    account: Account,
    cutoff_at: datetime,
    *,
    for_update: bool,
) -> list[FinancialTransaction]:
    statement = (
        select(FinancialTransaction)
        .where(
            FinancialTransaction.workspace_id == context.workspace.id,
            FinancialTransaction.status.in_(EFFECTIVE_STATUSES),
            FinancialTransaction.deleted_at.is_(None),
            FinancialTransaction.occurred_at >= account.opening_balance_at,
            FinancialTransaction.occurred_at < cutoff_at,
            or_(
                FinancialTransaction.account_id == account.id,
                FinancialTransaction.target_account_id == account.id,
            ),
        )
        .order_by(FinancialTransaction.occurred_at, FinancialTransaction.id)
    )
    if for_update:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


async def _transaction_effects(
    session: AsyncSession,
    account: Account,
    transactions: list[FinancialTransaction],
) -> dict[uuid.UUID, Decimal]:
    related_ids = {
        item.related_transaction_id
        for item in transactions
        if item.transaction_type == "refund" and item.related_transaction_id is not None
    }
    originals: dict[uuid.UUID, FinancialTransaction] = {}
    if related_ids:
        rows = list(
            (
                await session.scalars(
                    select(FinancialTransaction).where(
                        FinancialTransaction.id.in_(related_ids),
                        FinancialTransaction.workspace_id == account.workspace_id,
                    )
                )
            ).all()
        )
        originals = {item.id: item for item in rows}

    effects: dict[uuid.UUID, Decimal] = {}
    for item in transactions:
        if item.currency != account.currency:
            raise ApiError(
                status_code=409,
                code="TRANSACTION_CURRENCY_CONFLICT",
                message="An account transaction has an unexpected currency",
                details={"transaction_id": str(item.id)},
            )
        effect = Decimal("0")
        if item.transaction_type == "income" and item.account_id == account.id:
            effect = item.amount
        elif item.transaction_type == "expense" and item.account_id == account.id:
            effect = -item.amount
        elif item.transaction_type == "transfer":
            if item.account_id == account.id:
                effect -= item.amount
            if item.target_account_id == account.id:
                effect += item.amount
        elif item.transaction_type == "adjustment" and item.account_id == account.id:
            effect = item.amount
        elif item.transaction_type == "refund" and item.account_id == account.id:
            related_id = item.related_transaction_id
            original = originals.get(related_id) if related_id is not None else None
            if original is not None and original.transaction_type == "expense":
                effect = item.amount
            elif original is not None and original.transaction_type == "income":
                effect = -item.amount
        effects[item.id] = _money(effect)
    return effects


def _is_candidate(
    account_id: uuid.UUID,
    transaction: FinancialTransaction,
    linked_accounts: dict[uuid.UUID, set[uuid.UUID]],
) -> bool:
    linked = linked_accounts.get(transaction.id, set())
    if account_id in linked:
        return False
    if transaction.status == "confirmed":
        return True
    return (
        transaction.status == "reconciled"
        and transaction.transaction_type == "transfer"
        and bool(linked)
    )


def _preview_token(
    *,
    context: RequestContext,
    account: Account,
    data: AccountReconciliationPreviewRequest,
    cutoff_at: datetime,
    calculated_balance: Decimal,
    transactions: list[FinancialTransaction],
    candidates: list[FinancialTransaction],
) -> str:
    payload = {
        "workspace_id": str(context.workspace.id),
        "account_id": str(account.id),
        "account_version": account.version,
        "statement_date": data.statement_date.isoformat(),
        "statement_balance": str(_money(data.statement_balance)),
        "currency": data.currency,
        "cutoff_at": cutoff_at.isoformat(),
        "calculated_balance": str(calculated_balance),
        "effective_transactions": [
            [str(item.id), item.version, item.status] for item in transactions
        ],
        "candidate_ids": [str(item.id) for item in candidates],
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


async def _build_preview(
    session: AsyncSession,
    context: RequestContext,
    account: Account,
    data: AccountReconciliationPreviewRequest,
    *,
    for_update: bool,
) -> tuple[AccountReconciliationPreview, list[FinancialTransaction]]:
    _validate_request(context, account, data)
    cutoff_at = _cutoff_at(context, data.statement_date)
    transactions = await _effective_transactions(
        session, context, account, cutoff_at, for_update=for_update
    )
    linked_accounts = await repository.linked_accounts_by_transaction(
        session, context.workspace.id, [item.id for item in transactions]
    )
    effects = await _transaction_effects(session, account, transactions)
    calculated_balance = _money(
        account.opening_balance
        + sum((effects[item.id] for item in transactions), start=Decimal("0"))
    )
    candidates = [item for item in transactions if _is_candidate(account.id, item, linked_accounts)]
    statement_balance = _money(data.statement_balance)
    difference = _money(statement_balance - calculated_balance)
    token = _preview_token(
        context=context,
        account=account,
        data=data,
        cutoff_at=cutoff_at,
        calculated_balance=calculated_balance,
        transactions=transactions,
        candidates=candidates,
    )
    preview = AccountReconciliationPreview(
        account_id=account.id,
        statement_date=data.statement_date,
        cutoff_at=cutoff_at,
        statement_balance=statement_balance,
        calculated_balance=calculated_balance,
        difference=difference,
        currency=account.currency,
        account_version=account.version,
        preview_token=token,
        transactions=[
            AccountReconciliationTransaction(
                id=item.id,
                occurred_at=item.occurred_at,
                transaction_type=cast(TransactionType, item.transaction_type),
                amount=item.amount,
                signed_amount=effects[item.id],
                currency=item.currency,
                status=cast(TransactionStatus, item.status),
                counterparty=item.counterparty,
                description=item.description,
                version=item.version,
            )
            for item in candidates
        ],
    )
    return preview, candidates


async def preview_reconciliation(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
    data: AccountReconciliationPreviewRequest,
) -> AccountReconciliationPreview:
    account = await _account(session, context, account_id)
    preview, _ = await _build_preview(session, context, account, data, for_update=False)
    return preview


def _same_idempotent_request(
    record: AccountReconciliation,
    account_id: uuid.UUID,
    data: AccountReconciliationConfirmRequest,
) -> bool:
    return (
        record.account_id == account_id
        and record.statement_date == data.statement_date
        and record.statement_balance == _money(data.statement_balance)
        and record.currency == data.currency
        and record.account_version == data.account_version
        and record.preview_token == data.preview_token
    )


async def reconciliation_response(
    session: AsyncSession, record: AccountReconciliation
) -> AccountReconciliationResponse:
    return AccountReconciliationResponse(
        id=record.id,
        account_id=record.account_id,
        statement_date=record.statement_date,
        statement_balance=record.statement_balance,
        calculated_balance=record.calculated_balance,
        difference=record.difference,
        currency=record.currency,
        status="confirmed",
        account_version=record.account_version,
        version=record.version,
        created_by=record.created_by,
        confirmed_by=record.confirmed_by,
        created_at=record.created_at,
        confirmed_at=record.confirmed_at,
        transaction_ids=await repository.transaction_ids(session, record.id),
    )


async def _idempotent_result_or_conflict(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
    data: AccountReconciliationConfirmRequest,
) -> AccountReconciliationResponse | None:
    existing = await repository.get_by_idempotency_key(
        session, context.workspace.id, data.idempotency_key
    )
    if existing is None:
        return None
    if not _same_idempotent_request(existing, account_id, data):
        raise ApiError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message="Idempotency key was already used for another reconciliation",
        )
    return await reconciliation_response(session, existing)


async def confirm_reconciliation(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
    data: AccountReconciliationConfirmRequest,
) -> AccountReconciliationResponse:
    existing = await _idempotent_result_or_conflict(session, context, account_id, data)
    if existing is not None:
        return existing

    try:
        # A reconciliation is allowed after month close because confirmed and
        # reconciled transactions are financially equivalent.  Taking the
        # control-row lock still serializes it with close confirmation and
        # ledger writes, so the close fingerprint cannot observe a torn state.
        await get_or_create_control(session, context.workspace.id, for_update=True)
        account = await _account(session, context, account_id, for_update=True)
        preview, candidates = await _build_preview(session, context, account, data, for_update=True)
        if preview.preview_token != data.preview_token:
            raise ApiError(
                status_code=409,
                code="RECONCILIATION_PREVIEW_STALE",
                message="Transactions changed after preview; create a new preview",
            )
        if preview.difference != Decimal("0.0000"):
            raise ApiError(
                status_code=409,
                code="RECONCILIATION_DIFFERENCE",
                message="Reconciliation can be confirmed only when the difference is zero",
                details={"difference": str(preview.difference)},
            )

        now = datetime.now(UTC)
        record = AccountReconciliation(
            workspace_id=context.workspace.id,
            account_id=account.id,
            statement_date=preview.statement_date,
            statement_balance=preview.statement_balance,
            calculated_balance=preview.calculated_balance,
            difference=preview.difference,
            currency=preview.currency,
            status="confirmed",
            preview_token=preview.preview_token,
            idempotency_key=data.idempotency_key,
            account_version=account.version,
            version=1,
            created_by=context.user.id,
            confirmed_by=context.user.id,
            created_at=now,
            confirmed_at=now,
        )
        session.add(record)
        await session.flush()

        for transaction in candidates:
            before = snapshot("transaction", transaction)
            session.add(
                AccountReconciliationItem(
                    reconciliation_id=record.id,
                    transaction_id=transaction.id,
                    transaction_version=transaction.version,
                )
            )
            transaction.status = "reconciled"
            transaction.updated_by = context.user.id
            transaction.updated_at = now
            transaction.version += 1
            await session.flush()
            await record_audit(
                session,
                workspace_id=context.workspace.id,
                actor_user_id=context.user.id,
                entity_type="transaction",
                entity_id=transaction.id,
                action="reconcile",
                before_data=before,
                after_data=snapshot("transaction", transaction),
                request_id=context.request_id,
            )
            await enqueue_entity(
                session,
                workspace_id=context.workspace.id,
                entity_type="transaction",
                entity=transaction,
            )

        account_before = snapshot("account", account)
        account.version += 1
        account.updated_at = now
        await session.flush()
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="account",
            entity_id=account.id,
            action="reconcile",
            before_data=account_before,
            after_data=snapshot("account", account),
            request_id=context.request_id,
        )
        await enqueue_entity(
            session,
            workspace_id=context.workspace.id,
            entity_type="account",
            entity=account,
        )
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="account_reconciliation",
            entity_id=record.id,
            action="reconcile",
            before_data=None,
            after_data={
                "account_id": str(record.account_id),
                "statement_date": record.statement_date.isoformat(),
                "statement_balance": str(record.statement_balance),
                "calculated_balance": str(record.calculated_balance),
                "currency": record.currency,
                "transaction_count": len(candidates),
            },
            request_id=context.request_id,
        )
        await session.commit()
        return await reconciliation_response(session, record)
    except IntegrityError:
        await session.rollback()
        collision = await _idempotent_result_or_conflict(session, context, account_id, data)
        if collision is not None:
            return collision
        raise
    except Exception:
        await session.rollback()
        raise


async def get_reconciliation(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
) -> AccountReconciliationResponse:
    await _account(session, context, account_id)
    record = await repository.get_reconciliation(
        session, context.workspace.id, account_id, reconciliation_id
    )
    if record is None:
        raise ApiError(
            status_code=404,
            code="ACCOUNT_RECONCILIATION_NOT_FOUND",
            message="Account reconciliation was not found",
        )
    return await reconciliation_response(session, record)


async def list_reconciliations(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[AccountReconciliationResponse], int]:
    await _account(session, context, account_id)
    records, total = await repository.list_reconciliations(
        session,
        context.workspace.id,
        account_id,
        limit=limit,
        offset=offset,
    )
    return [await reconciliation_response(session, item) for item in records], total
