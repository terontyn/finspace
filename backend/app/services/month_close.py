import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.errors import ApiError
from app.db.models.automations import MonthClosure, RecurringRule, RecurringRuleExecution
from app.db.models.google_sync import SyncConflict, SyncOutbox
from app.db.models.imports import ImportBatch, ImportRow
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import Workspace
from app.dependencies.context import RequestContext
from app.services.audit import record_audit
from app.services.backup_status import get_backup_status
from app.services.calculations import calculate_balances


def period_date(year: int, month: int) -> date:
    if year < 2000 or year > 2200 or month < 1 or month > 12:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Month close period is invalid",
        )
    return date(year, month, 1)


async def get_closure(session: AsyncSession, workspace_id: uuid.UUID, period: date) -> MonthClosure:
    closure = await session.scalar(
        select(MonthClosure).where(
            MonthClosure.workspace_id == workspace_id,
            MonthClosure.period_month == period,
        )
    )
    if closure is None:
        raise ApiError(
            status_code=404,
            code="MONTH_CLOSE_NOT_FOUND",
            message="Month close was not prepared",
        )
    return closure


async def list_closures(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[MonthClosure], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(MonthClosure)
            .where(MonthClosure.workspace_id == workspace_id)
        )
        or 0
    )
    items = list(
        (
            await session.scalars(
                select(MonthClosure)
                .where(MonthClosure.workspace_id == workspace_id)
                .order_by(MonthClosure.period_month.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def prepare(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    *,
    actor_user_id: uuid.UUID | None,
    request_id: str,
    source: str,
) -> MonthClosure:
    closure = await session.scalar(
        select(MonthClosure).where(
            MonthClosure.workspace_id == workspace.id,
            MonthClosure.period_month == period,
        )
    )
    if closure is not None and closure.status == "confirmed":
        raise ApiError(
            status_code=409,
            code="MONTH_ALREADY_CLOSED",
            message="Month is already closed",
        )
    summary, blocking, warnings = await collect_issues(session, workspace, period)
    now = datetime.now(UTC)
    if closure is None:
        closure = MonthClosure(
            workspace_id=workspace.id,
            period_month=period,
            status="blocked" if blocking else "ready",
            prepared_by=actor_user_id,
            prepared_at=now,
            summary=summary,
            blocking_issues=blocking,
            warning_issues=warnings,
        )
        session.add(closure)
    else:
        closure.status = "blocked" if blocking else "ready"
        closure.prepared_by = actor_user_id
        closure.prepared_at = now
        closure.summary = summary
        closure.blocking_issues = blocking
        closure.warning_issues = warnings
        closure.version += 1
        closure.updated_at = now
    await session.flush()
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=actor_user_id,
        entity_type="month_closure",
        entity_id=closure.id,
        action="month_close.prepare",
        before_data=None,
        after_data={
            "period": period.isoformat(),
            "status": closure.status,
            "blocking_count": len(blocking),
            "warning_count": len(warnings),
        },
        request_id=request_id,
        source=source,
    )
    await session.commit()
    await session.refresh(closure)
    return closure


async def confirm(
    session: AsyncSession,
    context: RequestContext,
    period: date,
    *,
    version: int,
    explicit: bool,
) -> MonthClosure:
    closure = await get_closure(session, context.workspace.id, period)
    if closure.status == "confirmed":
        return closure
    if not explicit:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Explicit month close confirmation is required",
        )
    if closure.version != version:
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_VERSION_CONFLICT",
            message="Month close version is stale",
        )
    summary, blocking, warnings = await collect_issues(session, context.workspace, period)
    if blocking:
        closure.status = "blocked"
        closure.summary = summary
        closure.blocking_issues = blocking
        closure.warning_issues = warnings
        closure.version += 1
        closure.updated_at = datetime.now(UTC)
        await session.commit()
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_BLOCKED",
            message="Month close has blocking issues",
            details={"issues": blocking},
        )
    now = datetime.now(UTC)
    closure.status = "confirmed"
    closure.confirmed_by = context.user.id
    closure.confirmed_at = now
    closure.summary = summary
    closure.blocking_issues = []
    closure.warning_issues = warnings
    closure.version += 1
    closure.updated_at = now
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="month_closure",
        entity_id=closure.id,
        action="month_close.confirm",
        before_data=None,
        after_data={"period": period.isoformat(), "status": "confirmed"},
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(closure)
    return closure


async def reopen(
    session: AsyncSession,
    context: RequestContext,
    period: date,
    *,
    version: int,
    reason: str,
) -> MonthClosure:
    closure = await get_closure(session, context.workspace.id, period)
    if closure.version != version:
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_VERSION_CONFLICT",
            message="Month close version is stale",
        )
    if closure.status != "confirmed":
        return closure
    await _apply_reopen(
        session,
        closure,
        actor_user_id=context.user.id,
        request_id=context.request_id,
        reason=reason,
    )
    await session.commit()
    await session.refresh(closure)
    return closure


async def reopen_for_transaction(
    session: AsyncSession,
    context: RequestContext,
    occurred_at: datetime,
    *,
    transaction_id: uuid.UUID,
) -> None:
    local = occurred_at.astimezone(ZoneInfo(context.workspace.timezone))
    period = date(local.year, local.month, 1)
    closure = await session.scalar(
        select(MonthClosure).where(
            MonthClosure.workspace_id == context.workspace.id,
            MonthClosure.period_month == period,
            MonthClosure.status == "confirmed",
        )
    )
    if closure is None:
        return
    await _apply_reopen(
        session,
        closure,
        actor_user_id=context.user.id,
        request_id=context.request_id,
        reason=f"transaction:{transaction_id}",
    )


async def _apply_reopen(
    session: AsyncSession,
    closure: MonthClosure,
    *,
    actor_user_id: uuid.UUID,
    request_id: str,
    reason: str,
) -> None:
    closure.status = "reopened"
    closure.version += 1
    closure.updated_at = datetime.now(UTC)
    warnings = list(closure.warning_issues or [])
    warnings.append({"code": "MONTH_REOPENED", "reason": reason[:500]})
    closure.warning_issues = warnings
    await record_audit(
        session,
        workspace_id=closure.workspace_id,
        actor_user_id=actor_user_id,
        entity_type="month_closure",
        entity_id=closure.id,
        action="month_close.reopen",
        before_data={"status": "confirmed"},
        after_data={"status": "reopened", "reason": reason[:500]},
        request_id=request_id,
    )


async def collect_issues(
    session: AsyncSession, workspace: Workspace, period: date
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    start = datetime(
        period.year,
        period.month,
        1,
        tzinfo=ZoneInfo(workspace.timezone),
    ).astimezone(UTC)
    next_period = (
        date(period.year + 1, 1, 1)
        if period.month == 12
        else date(period.year, period.month + 1, 1)
    )
    end = datetime(
        next_period.year,
        next_period.month,
        1,
        tzinfo=ZoneInfo(workspace.timezone),
    ).astimezone(UTC)
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace.id,
                    FinancialTransaction.occurred_at >= start,
                    FinancialTransaction.occurred_at < end,
                    FinancialTransaction.deleted_at.is_(None),
                )
            )
        ).all()
    )
    blocking: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    draft_count = sum(item.status == "draft" for item in transactions)
    uncategorized = sum(
        item.transaction_type in {"income", "expense"} and item.category_id is None
        for item in transactions
    )
    if draft_count:
        blocking.append({"code": "DRAFT_TRANSACTIONS", "count": draft_count})
    if uncategorized:
        warnings.append({"code": "UNCATEGORIZED_TRANSACTIONS", "count": uncategorized})
    open_conflicts = await _count(
        session,
        select(SyncConflict.id).where(
            SyncConflict.workspace_id == workspace.id,
            SyncConflict.status == "open",
        ),
    )
    failed_outbox = await _count(
        session,
        select(SyncOutbox.id).where(
            SyncOutbox.workspace_id == workspace.id,
            SyncOutbox.status == "failed",
        ),
    )
    failed_imports = await _count(
        session,
        select(ImportBatch.id).where(
            ImportBatch.workspace_id == workspace.id,
            ImportBatch.status.in_(("failed", "validation_failed", "rollback_conflict")),
        ),
    )
    invalid_import_rows = await _count(
        session,
        select(ImportRow.id)
        .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
        .where(
            ImportBatch.workspace_id == workspace.id,
            ImportRow.status.in_(("invalid", "error", "conflict")),
        ),
    )
    failed_recurring = await _count(
        session,
        select(RecurringRuleExecution.id)
        .join(RecurringRule, RecurringRule.id == RecurringRuleExecution.rule_id)
        .where(
            RecurringRule.workspace_id == workspace.id,
            RecurringRuleExecution.status == "failed",
            RecurringRuleExecution.scheduled_for >= start,
            RecurringRuleExecution.scheduled_for < end,
        ),
    )
    for code, count in (
        ("SYNC_CONFLICTS", open_conflicts),
        ("FAILED_SYNC_OUTBOX", failed_outbox),
        ("FAILED_IMPORTS", failed_imports + invalid_import_rows),
        ("FAILED_RECURRING", failed_recurring),
    ):
        if count:
            blocking.append({"code": code, "count": count})
    backup = await get_backup_status(session)
    if backup.status != "healthy":
        blocking.append({"code": "BACKUP_STALE", "status": backup.status})
    duplicate_keys = Counter(
        (
            item.occurred_at.replace(microsecond=0),
            item.transaction_type,
            item.amount,
            item.currency,
            item.account_id,
        )
        for item in transactions
        if item.status != "cancelled"
    )
    duplicate_count = sum(count - 1 for count in duplicate_keys.values() if count > 1)
    if duplicate_count:
        warnings.append({"code": "POSSIBLE_DUPLICATES", "count": duplicate_count})
    balances = await calculate_balances(session, workspace.id)
    negative = [item for item in balances if item.balance < Decimal("0")]
    if negative:
        warnings.append({"code": "NEGATIVE_BALANCES", "count": len(negative)})
    currency_totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"income": Decimal("0"), "expense": Decimal("0")}
    )
    for item in transactions:
        if item.status in {"confirmed", "reconciled"} and item.transaction_type in {
            "income",
            "expense",
        }:
            currency_totals[item.currency][item.transaction_type] += item.amount
    currency_summary = [
        {
            "currency": currency,
            "income": str(values["income"].quantize(Decimal("0.0001"))),
            "expense": str(values["expense"].quantize(Decimal("0.0001"))),
            "net_cashflow": str((values["income"] - values["expense"]).quantize(Decimal("0.0001"))),
        }
        for currency, values in sorted(currency_totals.items())
    ]
    summary: dict[str, object] = {
        "transaction_count": len(transactions),
        "draft_count": draft_count,
        "uncategorized_count": uncategorized,
        "possible_duplicate_count": duplicate_count,
        "negative_balance_count": len(negative),
        "backup_status": backup.status,
        "backup_verified_at": backup.last_verified_at.isoformat()
        if backup.last_verified_at
        else None,
        "currencies": currency_summary,
        "account_balances": [item.model_dump(mode="json") for item in balances],
    }
    return summary, blocking, warnings


async def _count(session: AsyncSession, query: Select[tuple[uuid.UUID]]) -> int:
    return len(list((await session.scalars(query)).all()))
