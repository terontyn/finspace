import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.payees import Payee
from app.db.models.transactions import FinancialTransaction, TransactionSplit
from app.dependencies.context import RequestContext
from app.repositories import accounts as account_repository
from app.repositories import categories as category_repository
from app.repositories import payees as payee_repository
from app.repositories import transactions as repository
from app.schemas.transactions import (
    EntityRef,
    SplitInput,
    SplitResponse,
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
)
from app.services import payees as payee_service
from app.services.audit import AuditCause, record_audit, snapshot
from app.services.financial_period_guard import (
    assert_dates_open,
    get_or_create_control,
)
from app.services.sync_outbox import enqueue_entity


async def _account(
    session: AsyncSession, workspace_id: uuid.UUID, account_id: uuid.UUID
) -> Account:
    account = await account_repository.get_account(session, workspace_id, account_id)
    if account is None or account.is_archived:
        raise ApiError(status_code=404, code="ACCOUNT_NOT_FOUND", message="Account was not found")
    return account


async def _category(
    session: AsyncSession, workspace_id: uuid.UUID, category_id: uuid.UUID
) -> Category:
    category = await category_repository.get_category(session, workspace_id, category_id)
    if category is None or category.is_archived:
        raise ApiError(status_code=404, code="CATEGORY_NOT_FOUND", message="Category was not found")
    return category


def _allowed_category(transaction_type: str, category_type: str) -> bool:
    if transaction_type == "income":
        return category_type in {"income", "both"}
    if transaction_type == "expense":
        return category_type in {"expense", "both"}
    return True


async def _validate_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    data: TransactionCreate,
    *,
    current_id: uuid.UUID | None = None,
) -> None:
    if data.status == "reconciled":
        raise ApiError(
            status_code=409,
            code="RECONCILIATION_REQUIRED",
            message="Use account reconciliation to reconcile transactions",
        )
    source_account = await _account(session, workspace_id, data.account_id)
    if source_account.currency != data.currency:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Transaction currency must match account currency",
        )

    if data.transaction_type == "transfer":
        if data.target_account_id is None or data.target_account_id == data.account_id:
            raise ApiError(
                status_code=422,
                code="INVALID_TRANSFER",
                message="Transfer requires a different target account",
            )
        target_account = await _account(session, workspace_id, data.target_account_id)
        if target_account.currency != source_account.currency:
            raise ApiError(
                status_code=422,
                code="INVALID_TRANSFER",
                message="Transfers between different currencies are not supported",
            )
        if data.category_id is not None or data.splits:
            raise ApiError(
                status_code=422,
                code="INVALID_TRANSFER",
                message="Transfer cannot contain a category or splits",
            )
    elif data.target_account_id is not None:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Target account is allowed only for transfers",
        )

    if data.category_id is not None and data.splits:
        raise ApiError(
            status_code=422,
            code="INVALID_SPLIT_TOTAL",
            message="A split transaction cannot also have category_id",
        )
    if data.transaction_type in {"income", "expense"} and not (
        data.category_id is not None or data.splits
    ):
        raise ApiError(
            status_code=422,
            code="INVALID_CATEGORY_TYPE",
            message="Income and expense require a category or splits",
        )
    if data.category_id is not None:
        category = await _category(session, workspace_id, data.category_id)
        if not _allowed_category(data.transaction_type, category.category_type):
            raise ApiError(
                status_code=422,
                code="INVALID_CATEGORY_TYPE",
                message="Category type does not match transaction type",
            )
    if data.splits:
        if data.transaction_type == "transfer":
            raise ApiError(
                status_code=422,
                code="INVALID_TRANSFER",
                message="Transfer cannot be split",
            )
        total = sum((split.amount for split in data.splits), start=Decimal("0"))
        if total != data.amount:
            raise ApiError(
                status_code=422,
                code="INVALID_SPLIT_TOTAL",
                message="Split total must exactly match transaction amount",
                details={"amount": str(data.amount), "split_total": str(total)},
            )
        for split in data.splits:
            category = await _category(session, workspace_id, split.category_id)
            if not _allowed_category(data.transaction_type, category.category_type):
                raise ApiError(
                    status_code=422,
                    code="INVALID_CATEGORY_TYPE",
                    message="Split category type does not match transaction type",
                )

    if data.transaction_type == "refund":
        if data.related_transaction_id is None:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="Refund requires related_transaction_id",
            )
        original = await repository.get_transaction(
            session, workspace_id, data.related_transaction_id
        )
        if original is None or original.transaction_type not in {"income", "expense"}:
            raise ApiError(
                status_code=422,
                code="TRANSACTION_NOT_FOUND",
                message="Refund source transaction was not found",
            )
        if data.account_id != original.account_id or data.currency != original.currency:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="Refund must use the original account and currency",
            )
        refund_filters = [
            FinancialTransaction.workspace_id == workspace_id,
            FinancialTransaction.related_transaction_id == original.id,
            FinancialTransaction.transaction_type == "refund",
            FinancialTransaction.status != "cancelled",
            FinancialTransaction.deleted_at.is_(None),
        ]
        if current_id is not None:
            refund_filters.append(FinancialTransaction.id != current_id)
        refunded = await session.scalar(
            select(func.coalesce(func.sum(FinancialTransaction.amount), 0)).where(*refund_filters)
        )
        if Decimal(refunded or 0) + data.amount > original.amount:
            raise ApiError(
                status_code=422,
                code="REFUND_LIMIT_EXCEEDED",
                message="Refund total exceeds the original transaction amount",
            )

    if data.transaction_type == "adjustment" and not (data.comment or "").strip():
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Adjustment requires a clear comment",
        )


async def _replace_splits(
    session: AsyncSession,
    transaction_id: uuid.UUID,
    splits: list[SplitInput],
) -> None:
    await session.execute(
        delete(TransactionSplit).where(TransactionSplit.transaction_id == transaction_id)
    )
    session.add_all(
        [
            TransactionSplit(
                transaction_id=transaction_id,
                category_id=item.category_id,
                amount=item.amount,
                comment=item.comment,
            )
            for item in splits
        ]
    )
    await session.flush()


async def create_transaction(
    session: AsyncSession,
    context: RequestContext,
    data: TransactionCreate,
    *,
    commit: bool = True,
    audit_source: str = "api",
) -> FinancialTransaction:
    control = await get_or_create_control(session, context.workspace.id, for_update=True)
    assert_dates_open(control, context.workspace.timezone, [data.occurred_at])
    if data.payee_id is not None:
        await payee_service.get_assignable_payee_for_write(
            session,
            context.workspace.id,
            data.payee_id,
        )
    await _validate_transaction(session, context.workspace.id, data)
    values = data.model_dump(exclude={"splits"})
    transaction = FinancialTransaction(
        workspace_id=context.workspace.id,
        created_by=context.user.id,
        updated_by=context.user.id,
        **values,
    )
    session.add(transaction)
    await session.flush()
    if data.splits:
        await _replace_splits(session, transaction.id, data.splits)
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="transaction",
        entity_id=transaction.id,
        action="create",
        before_data=None,
        after_data=snapshot("transaction", transaction),
        request_id=context.request_id,
        source=audit_source,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="transaction",
        entity=transaction,
    )
    if commit:
        await session.commit()
    return transaction


async def update_transaction(
    session: AsyncSession,
    context: RequestContext,
    transaction_id: uuid.UUID,
    data: TransactionUpdate,
    *,
    commit: bool = True,
    audit_source: str = "api",
    audit_cause: AuditCause | None = None,
) -> FinancialTransaction:
    control = await get_or_create_control(session, context.workspace.id, for_update=True)
    transaction = await repository.get_transaction(
        session, context.workspace.id, transaction_id, for_update=True
    )
    if transaction is None:
        raise ApiError(
            status_code=404,
            code="TRANSACTION_NOT_FOUND",
            message="Transaction was not found",
        )
    if transaction.version != data.version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    if transaction.status == "reconciled":
        raise ApiError(
            status_code=409,
            code="RECONCILED_TRANSACTION_IMMUTABLE",
            message="A reconciled transaction cannot be changed",
        )
    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    if "payee_id" in changes and changes["payee_id"] is not None:
        payee_id = changes["payee_id"]
        if not isinstance(payee_id, uuid.UUID):
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="Payee identifier is invalid",
            )
        await payee_service.get_assignable_payee_for_write(
            session,
            context.workspace.id,
            payee_id,
        )
    existing_splits = await repository.get_splits(session, transaction.id)
    current = {
        "occurred_at": transaction.occurred_at,
        "transaction_type": transaction.transaction_type,
        "amount": transaction.amount,
        "currency": transaction.currency,
        "account_id": transaction.account_id,
        "target_account_id": transaction.target_account_id,
        "category_id": transaction.category_id,
        "payee_id": transaction.payee_id,
        "counterparty": transaction.counterparty,
        "description": transaction.description,
        "comment": transaction.comment,
        "status": transaction.status,
        "source": transaction.source,
        "related_transaction_id": transaction.related_transaction_id,
        "external_id": transaction.external_id,
        "splits": [
            SplitInput(category_id=item.category_id, amount=item.amount, comment=item.comment)
            for item in existing_splits
        ],
    }
    if changes.get("status") == "reconciled":
        raise ApiError(
            status_code=409,
            code="RECONCILIATION_REQUIRED",
            message="Use account reconciliation to reconcile transactions",
        )
    current.update(changes)
    merged = TransactionCreate.model_validate(current)
    assert_dates_open(
        control,
        context.workspace.timezone,
        [transaction.occurred_at, merged.occurred_at],
    )
    await _validate_transaction(session, context.workspace.id, merged, current_id=transaction.id)
    before = snapshot("transaction", transaction)
    for field, value in changes.items():
        if field != "splits":
            setattr(transaction, field, value)
    transaction.updated_by = context.user.id
    transaction.updated_at = datetime.now(UTC)
    transaction.version += 1
    if "splits" in changes:
        await _replace_splits(session, transaction.id, merged.splits)
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="transaction",
        entity_id=transaction.id,
        action="update",
        before_data=before,
        after_data=snapshot("transaction", transaction),
        request_id=context.request_id,
        source=audit_source,
        cause=audit_cause,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="transaction",
        entity=transaction,
    )
    if commit:
        await session.commit()
    return transaction


async def _change_lifecycle(
    session: AsyncSession,
    context: RequestContext,
    transaction_id: uuid.UUID,
    version: int,
    action: str,
) -> FinancialTransaction:
    control = await get_or_create_control(session, context.workspace.id, for_update=True)
    transaction = await repository.get_transaction(
        session,
        context.workspace.id,
        transaction_id,
        include_deleted=action == "restore",
        for_update=True,
    )
    if transaction is None:
        raise ApiError(
            status_code=404,
            code="TRANSACTION_NOT_FOUND",
            message="Transaction was not found",
        )
    if transaction.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    if transaction.status == "reconciled" and action != "restore":
        raise ApiError(
            status_code=409,
            code="RECONCILED_TRANSACTION_IMMUTABLE",
            message="A reconciled transaction cannot be changed",
        )
    assert_dates_open(control, context.workspace.timezone, [transaction.occurred_at])
    before = snapshot("transaction", transaction)
    if action == "delete":
        transaction.deleted_at = datetime.now(UTC)
    elif action == "restore":
        transaction.deleted_at = None
    elif action == "cancel":
        transaction.status = "cancelled"
    transaction.updated_by = context.user.id
    transaction.version += 1
    transaction.updated_at = datetime.now(UTC)
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="transaction",
        entity_id=transaction.id,
        action=action,
        before_data=before,
        after_data=snapshot("transaction", transaction),
        request_id=context.request_id,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="transaction",
        entity=transaction,
        operation={"delete": "delete", "restore": "restore", "cancel": "upsert"}[action],
    )
    await session.commit()
    return transaction


async def delete_transaction(
    session: AsyncSession, context: RequestContext, transaction_id: uuid.UUID, version: int
) -> FinancialTransaction:
    return await _change_lifecycle(session, context, transaction_id, version, action="delete")


async def restore_transaction(
    session: AsyncSession, context: RequestContext, transaction_id: uuid.UUID, version: int
) -> FinancialTransaction:
    return await _change_lifecycle(session, context, transaction_id, version, action="restore")


async def cancel_transaction(
    session: AsyncSession, context: RequestContext, transaction_id: uuid.UUID, version: int
) -> FinancialTransaction:
    return await _change_lifecycle(session, context, transaction_id, version, action="cancel")


async def confirm_transaction(
    session: AsyncSession,
    context: RequestContext,
    transaction_id: uuid.UUID,
    version: int,
) -> FinancialTransaction:
    control = await get_or_create_control(session, context.workspace.id, for_update=True)
    transaction = await repository.get_transaction(
        session, context.workspace.id, transaction_id, for_update=True
    )
    if transaction is None:
        raise ApiError(
            status_code=404,
            code="TRANSACTION_NOT_FOUND",
            message="Transaction was not found",
        )
    if transaction.status == "confirmed":
        return transaction
    if transaction.status != "draft":
        raise ApiError(
            status_code=409,
            code="VALIDATION_ERROR",
            message="Only a draft transaction can be confirmed",
        )
    assert_dates_open(control, context.workspace.timezone, [transaction.occurred_at])
    return await update_transaction(
        session,
        context,
        transaction_id,
        TransactionUpdate(version=version, status="confirmed"),
    )


async def transaction_response(
    session: AsyncSession,
    transaction: FinancialTransaction,
    *,
    payees_by_id: dict[uuid.UUID, Payee] | None = None,
) -> TransactionResponse:
    source_account = await session.get(Account, transaction.account_id)
    target_account = (
        await session.get(Account, transaction.target_account_id)
        if transaction.target_account_id is not None
        else None
    )
    category = (
        await session.get(Category, transaction.category_id)
        if transaction.category_id is not None
        else None
    )
    payee = (
        (
            payees_by_id.get(transaction.payee_id)
            if payees_by_id is not None
            else await session.get(Payee, transaction.payee_id)
        )
        if transaction.payee_id is not None
        else None
    )
    split_rows = await repository.get_splits(session, transaction.id)
    split_responses: list[SplitResponse] = []
    for item in split_rows:
        split_category = await session.get(Category, item.category_id)
        split_responses.append(
            SplitResponse(
                id=item.id,
                category_id=item.category_id,
                category_name=split_category.name if split_category is not None else "Удалена",
                amount=item.amount,
                comment=item.comment,
            )
        )
    if source_account is None:
        raise ApiError(status_code=500, code="INTERNAL_ERROR", message="Source account is missing")
    return TransactionResponse(
        id=transaction.id,
        occurred_at=transaction.occurred_at,
        transaction_type=transaction.transaction_type,  # type: ignore[arg-type]
        amount=transaction.amount,
        currency=transaction.currency,
        account=EntityRef(id=source_account.id, name=source_account.name),
        target_account=(
            EntityRef(id=target_account.id, name=target_account.name)
            if target_account is not None
            else None
        ),
        category=(EntityRef(id=category.id, name=category.name) if category is not None else None),
        payee=(EntityRef(id=payee.id, name=payee.name) if payee is not None else None),
        counterparty=transaction.counterparty,
        description=transaction.description,
        comment=transaction.comment,
        status=transaction.status,  # type: ignore[arg-type]
        source=transaction.source,  # type: ignore[arg-type]
        related_transaction_id=transaction.related_transaction_id,
        external_id=transaction.external_id,
        splits=split_responses,
        version=transaction.version,
        created_at=transaction.created_at,
        updated_at=transaction.updated_at,
    )


async def transaction_page_responses(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transactions: list[FinancialTransaction],
) -> list[TransactionResponse]:
    payee_ids = {item.payee_id for item in transactions if item.payee_id is not None}
    payees_by_id = await payee_repository.get_payees_by_ids(session, workspace_id, payee_ids)
    return [
        await transaction_response(session, item, payees_by_id=payees_by_id)
        for item in transactions
    ]
