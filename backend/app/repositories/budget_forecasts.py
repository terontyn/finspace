import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.automations import (
    MonthCloseRevision,
    MonthClosure,
    RecurringRule,
    RecurringRuleExecution,
)
from app.db.models.categories import Category
from app.db.models.transactions import FinancialTransaction, TransactionSplit


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    execution: RecurringRuleExecution
    transaction: FinancialTransaction | None


@dataclass(frozen=True, slots=True)
class PendingDraftRecord:
    execution: RecurringRuleExecution
    transaction: FinancialTransaction
    rule: RecurringRule


async def confirmed_planning_snapshot(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
) -> dict[str, object] | None:
    revision = await session.scalar(
        select(MonthCloseRevision)
        .join(MonthClosure, MonthClosure.current_revision_id == MonthCloseRevision.id)
        .where(
            MonthClosure.workspace_id == workspace_id,
            MonthClosure.period_month == period,
            MonthClosure.status == "confirmed",
            MonthCloseRevision.workspace_id == workspace_id,
        )
    )
    if revision is None:
        return None
    value = revision.snapshot.get("planning_budget")
    return dict(value) if isinstance(value, dict) else {}


async def active_rules(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    currency: str,
) -> list[RecurringRule]:
    return list(
        (
            await session.scalars(
                select(RecurringRule)
                .where(
                    RecurringRule.workspace_id == workspace_id,
                    RecurringRule.currency == currency,
                    RecurringRule.is_active.is_(True),
                    RecurringRule.deleted_at.is_(None),
                    RecurringRule.next_run_at.is_not(None),
                )
                .order_by(RecurringRule.next_run_at, RecurringRule.id)
            )
        ).all()
    )


async def execution_records(
    session: AsyncSession,
    rule_ids: list[uuid.UUID],
    *,
    start: datetime,
    end: datetime,
    cursor_keys: list[tuple[uuid.UUID, datetime]],
) -> list[ExecutionRecord]:
    if not rule_ids:
        return []
    period_filter = and_(
        RecurringRuleExecution.rule_id.in_(rule_ids),
        RecurringRuleExecution.scheduled_for >= start,
        RecurringRuleExecution.scheduled_for < end,
    )
    filters = [period_filter]
    if cursor_keys:
        filters.append(
            tuple_(
                RecurringRuleExecution.rule_id,
                RecurringRuleExecution.scheduled_for,
            ).in_(cursor_keys)
        )
    rows = (
        await session.execute(
            select(RecurringRuleExecution, FinancialTransaction)
            .outerjoin(
                FinancialTransaction,
                FinancialTransaction.id == RecurringRuleExecution.transaction_id,
            )
            .where(or_(*filters))
            .order_by(
                RecurringRuleExecution.scheduled_for,
                RecurringRuleExecution.rule_id,
                RecurringRuleExecution.id,
            )
        )
    ).all()
    return [ExecutionRecord(row[0], row[1]) for row in rows]


async def pending_linked_drafts(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    currency: str,
    *,
    start: datetime,
    end: datetime,
) -> list[PendingDraftRecord]:
    rows = (
        await session.execute(
            select(RecurringRuleExecution, FinancialTransaction, RecurringRule)
            .join(
                FinancialTransaction,
                FinancialTransaction.id == RecurringRuleExecution.transaction_id,
            )
            .join(RecurringRule, RecurringRule.id == RecurringRuleExecution.rule_id)
            .where(
                FinancialTransaction.workspace_id == workspace_id,
                RecurringRule.workspace_id == workspace_id,
                FinancialTransaction.occurred_at >= start,
                FinancialTransaction.occurred_at < end,
                FinancialTransaction.currency == currency,
                FinancialTransaction.status == "draft",
                FinancialTransaction.deleted_at.is_(None),
            )
            .order_by(
                FinancialTransaction.occurred_at,
                RecurringRule.id,
                RecurringRuleExecution.id,
            )
        )
    ).all()
    return [PendingDraftRecord(row[0], row[1], row[2]) for row in rows]


async def splits_for_transactions(
    session: AsyncSession,
    transaction_ids: set[uuid.UUID],
) -> dict[uuid.UUID, list[TransactionSplit]]:
    result: dict[uuid.UUID, list[TransactionSplit]] = {
        transaction_id: [] for transaction_id in transaction_ids
    }
    if not transaction_ids:
        return result
    rows = list(
        (
            await session.scalars(
                select(TransactionSplit)
                .where(TransactionSplit.transaction_id.in_(transaction_ids))
                .order_by(TransactionSplit.transaction_id, TransactionSplit.id)
            )
        ).all()
    )
    for row in rows:
        result[row.transaction_id].append(row)
    return result


async def categories(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    category_ids: set[uuid.UUID],
) -> dict[uuid.UUID, Category]:
    if not category_ids:
        return {}
    rows = (
        await session.scalars(
            select(Category).where(
                Category.workspace_id == workspace_id,
                Category.id.in_(category_ids),
            )
        )
    ).all()
    return {row.id: row for row in rows}
