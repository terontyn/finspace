import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.automations import MonthCloseControl
from app.db.models.transactions import FinancialTransaction

EFFECTIVE_STATUSES = ("confirmed", "reconciled")


def next_month(period: date) -> date:
    return (
        date(period.year + 1, 1, 1)
        if period.month == 12
        else date(period.year, period.month + 1, 1)
    )


def previous_month(period: date) -> date:
    return (
        date(period.year - 1, 12, 1)
        if period.month == 1
        else date(period.year, period.month - 1, 1)
    )


def month_end(period: date) -> date:
    return next_month(period) - timedelta(days=1)


def period_bounds(period: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(period, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(next_month(period), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


async def get_or_create_control(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    for_update: bool,
) -> MonthCloseControl:
    await session.execute(
        insert(MonthCloseControl)
        .values(workspace_id=workspace_id, backup_policy="warn", version=1)
        .on_conflict_do_nothing(index_elements=[MonthCloseControl.workspace_id])
    )
    statement = select(MonthCloseControl).where(MonthCloseControl.workspace_id == workspace_id)
    if for_update:
        statement = statement.with_for_update()
    control = await session.scalar(statement)
    if control is None:
        raise RuntimeError("Month close control row could not be created")
    return control


def _local_date(value: datetime | date, timezone: str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(timezone)).date()
    return value


def closed_dates(
    control: MonthCloseControl,
    timezone: str,
    affected_dates: Iterable[datetime | date],
) -> list[date]:
    """Return the affected local dates that fall inside the closed period.

    Extracted so advisory callers (such as bulk categorization preview classification) can ask the
    same question without raising and without taking the exclusive month-close lock.
    """
    if control.closed_through is None:
        return []
    local_dates = sorted({_local_date(item, timezone) for item in affected_dates})
    return [item for item in local_dates if item <= control.closed_through]


def assert_dates_open(
    control: MonthCloseControl,
    timezone: str,
    affected_dates: Iterable[datetime | date],
) -> None:
    closed = closed_dates(control, timezone, affected_dates)
    if not closed:
        return
    raise ApiError(
        status_code=409,
        code="MONTH_CLOSED",
        message="Financial history is closed for the affected period",
        details={
            "closed_through": (
                control.closed_through.isoformat() if control.closed_through else None
            ),
            "affected_periods": sorted({item.strftime("%Y-%m") for item in closed}),
        },
    )


async def lock_and_assert_dates_open(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    timezone: str,
    affected_dates: Iterable[datetime | date],
) -> MonthCloseControl:
    control = await get_or_create_control(session, workspace_id, for_update=True)
    assert_dates_open(control, timezone, affected_dates)
    return control


async def account_affects_closed_history(
    session: AsyncSession,
    control: MonthCloseControl,
    account: Account,
    timezone: str,
) -> list[datetime]:
    if control.closed_through is None:
        return []
    _, cutoff = period_bounds(
        date(control.closed_through.year, control.closed_through.month, 1), timezone
    )
    affected: list[datetime] = []
    # Account visibility is part of the durable close fingerprint even when the
    # opening balance is zero, so deleting/restoring a historical account must
    # be guarded just like a non-zero account.
    if account.opening_balance_at < cutoff:
        affected.append(account.opening_balance_at)
    first_transaction = await session.scalar(
        select(FinancialTransaction.occurred_at)
        .where(
            FinancialTransaction.workspace_id == account.workspace_id,
            FinancialTransaction.occurred_at < cutoff,
            FinancialTransaction.status.in_(EFFECTIVE_STATUSES),
            FinancialTransaction.deleted_at.is_(None),
            or_(
                FinancialTransaction.account_id == account.id,
                FinancialTransaction.target_account_id == account.id,
            ),
        )
        .order_by(FinancialTransaction.occurred_at)
        .limit(1)
    )
    if first_transaction is not None:
        affected.append(first_transaction)
    return affected
