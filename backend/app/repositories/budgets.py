import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.budgets import BudgetAllocation, BudgetPeriod, BudgetPlanRevision


async def get_period(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    currency: str,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> BudgetPeriod | None:
    filters = [
        BudgetPeriod.workspace_id == workspace_id,
        BudgetPeriod.period_month == period,
        BudgetPeriod.currency == currency,
    ]
    if not include_deleted:
        filters.append(BudgetPeriod.deleted_at.is_(None))
    statement = select(BudgetPeriod).where(*filters)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def list_periods(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> list[BudgetPeriod]:
    filters = [BudgetPeriod.workspace_id == workspace_id, BudgetPeriod.period_month == period]
    if not include_deleted:
        filters.append(BudgetPeriod.deleted_at.is_(None))
    statement = (
        select(BudgetPeriod).where(*filters).order_by(BudgetPeriod.currency, BudgetPeriod.id)
    )
    if for_update:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


async def lock_period_keys(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    keys: set[tuple[date, str]],
) -> dict[tuple[date, str], BudgetPeriod]:
    """Lock existing Budget rows in the canonical period/currency/id order."""
    if not keys:
        return {}
    periods = {period for period, _ in keys}
    currencies = {currency for _, currency in keys}
    rows = list(
        (
            await session.scalars(
                select(BudgetPeriod)
                .where(
                    BudgetPeriod.workspace_id == workspace_id,
                    BudgetPeriod.period_month.in_(periods),
                    BudgetPeriod.currency.in_(currencies),
                )
                .order_by(
                    BudgetPeriod.period_month,
                    BudgetPeriod.currency,
                    BudgetPeriod.id,
                )
                .with_for_update()
            )
        ).all()
    )
    return {
        (row.period_month, row.currency): row
        for row in rows
        if (row.period_month, row.currency) in keys
    }


async def allocations_for_periods(
    session: AsyncSession, period_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[BudgetAllocation]]:
    result: dict[uuid.UUID, list[BudgetAllocation]] = {period_id: [] for period_id in period_ids}
    if not period_ids:
        return result
    rows = list(
        (
            await session.scalars(
                select(BudgetAllocation)
                .where(BudgetAllocation.budget_period_id.in_(period_ids))
                .order_by(BudgetAllocation.budget_period_id, BudgetAllocation.category_id)
            )
        ).all()
    )
    for row in rows:
        result.setdefault(row.budget_period_id, []).append(row)
    return result


async def revision_for_key(
    session: AsyncSession, workspace_id: uuid.UUID, idempotency_key: str
) -> BudgetPlanRevision | None:
    return await session.scalar(
        select(BudgetPlanRevision).where(
            BudgetPlanRevision.workspace_id == workspace_id,
            BudgetPlanRevision.idempotency_key == idempotency_key,
        )
    )


async def next_revision_number(session: AsyncSession, budget_period_id: uuid.UUID) -> int:
    return (
        int(
            await session.scalar(
                select(func.coalesce(func.max(BudgetPlanRevision.revision_number), 0)).where(
                    BudgetPlanRevision.budget_period_id == budget_period_id
                )
            )
            or 0
        )
        + 1
    )


async def list_revisions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    budget_period_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[BudgetPlanRevision], int]:
    filters = (
        BudgetPlanRevision.workspace_id == workspace_id,
        BudgetPlanRevision.budget_period_id == budget_period_id,
    )
    total = int(
        await session.scalar(select(func.count()).select_from(BudgetPlanRevision).where(*filters))
        or 0
    )
    rows = list(
        (
            await session.scalars(
                select(BudgetPlanRevision)
                .where(*filters)
                .order_by(BudgetPlanRevision.revision_number.desc(), BudgetPlanRevision.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return rows, total
