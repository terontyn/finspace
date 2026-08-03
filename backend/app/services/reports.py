import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.automations import NotificationSetting, RecurringRule, TelegramLink
from app.db.models.categories import Category
from app.db.models.google_sync import GoogleSheetBinding, SyncConflict, SyncOutbox
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import Workspace, WorkspaceMember
from app.schemas.automations import (
    MoneyTotal,
    ReportRecipient,
    UncategorizedReportResponse,
    WeeklyReportGroup,
    WeeklyReportResponse,
)
from app.services.backup_status import get_backup_status
from app.services.calculations import calculate_balances

EFFECTIVE_STATUSES = ("confirmed", "reconciled")


async def weekly_report(
    session: AsyncSession, workspace: Workspace, week_start: date
) -> WeeklyReportResponse:
    zone = ZoneInfo(workspace.timezone)
    start = datetime.combine(week_start, time.min, tzinfo=zone).astimezone(UTC)
    end = start + timedelta(days=7)
    previous_start = start - timedelta(days=7)
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace.id,
                    FinancialTransaction.occurred_at >= previous_start,
                    FinancialTransaction.occurred_at < end,
                    FinancialTransaction.status.in_(EFFECTIVE_STATUSES),
                    FinancialTransaction.deleted_at.is_(None),
                )
            )
        ).all()
    )
    categories = {
        item.id: item.name
        for item in (
            await session.scalars(select(Category).where(Category.workspace_id == workspace.id))
        ).all()
    }
    totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "income": Decimal("0"),
            "expense": Decimal("0"),
            "previous_income": Decimal("0"),
            "previous_expense": Decimal("0"),
        }
    )
    category_totals: dict[tuple[str, uuid.UUID], Decimal] = defaultdict(lambda: Decimal("0"))
    uncategorized = 0
    for item in transactions:
        period = "current" if item.occurred_at >= start else "previous"
        if item.transaction_type == "income":
            key = "income" if period == "current" else "previous_income"
            totals[item.currency][key] += item.amount
        elif item.transaction_type == "expense":
            key = "expense" if period == "current" else "previous_expense"
            totals[item.currency][key] += item.amount
            if period == "current" and item.category_id is not None:
                category_totals[(item.currency, item.category_id)] += item.amount
        if (
            period == "current"
            and item.transaction_type in {"income", "expense"}
            and item.category_id is None
        ):
            uncategorized += 1
    groups: list[WeeklyReportGroup] = []
    for currency, values in sorted(totals.items()):
        top = sorted(
            (
                {
                    "category_id": str(category_id),
                    "name": categories.get(category_id, "Без категории"),
                    "amount": str(amount.quantize(Decimal("0.0001"))),
                }
                for (item_currency, category_id), amount in category_totals.items()
                if item_currency == currency
            ),
            key=lambda item: Decimal(item["amount"]),
            reverse=True,
        )[:5]
        groups.append(
            WeeklyReportGroup(
                currency=currency,
                income=_money(values["income"]),
                expense=_money(values["expense"]),
                net_cashflow=_money(values["income"] - values["expense"]),
                previous_income=_money(values["previous_income"]),
                previous_expense=_money(values["previous_expense"]),
                top_categories=top,
            )
        )
    draft_count = len(
        list(
            (
                await session.scalars(
                    select(FinancialTransaction.id).where(
                        FinancialTransaction.workspace_id == workspace.id,
                        FinancialTransaction.occurred_at >= start,
                        FinancialTransaction.occurred_at < end,
                        FinancialTransaction.status == "draft",
                        FinancialTransaction.deleted_at.is_(None),
                    )
                )
            ).all()
        )
    )
    upcoming = len(
        list(
            (
                await session.scalars(
                    select(RecurringRule.id).where(
                        RecurringRule.workspace_id == workspace.id,
                        RecurringRule.is_active.is_(True),
                        RecurringRule.deleted_at.is_(None),
                        RecurringRule.next_run_at >= end,
                        RecurringRule.next_run_at < end + timedelta(days=7),
                    )
                )
            ).all()
        )
    )
    balances = await calculate_balances(session, workspace.id)
    sync_status = await _sync_status(session, workspace.id)
    backup = await get_backup_status(session)
    recipients = await report_recipients(session, workspace.id, "weekly_report")
    report_end = week_start + timedelta(days=6)
    lines = [f"Финпространство — неделя {week_start:%d.%m} - {report_end:%d.%m}"]
    if not groups:
        lines.append("За период нет подтверждённых операций.")  # noqa: RUF001
    for group in groups:
        lines.extend(
            [
                "",
                group.currency,
                f"Доходы: {_format_money(group.income)}",
                f"Расходы: {_format_money(group.expense)}",
                f"Денежный поток: {_format_signed(group.net_cashflow)}",
                f"Прошлая неделя — доходы {_format_money(group.previous_income)}, "
                f"расходы {_format_money(group.previous_expense)}",
            ]
        )
        if group.top_categories:
            lines.append("Больше всего:")
            lines.extend(
                f"{index}. {item['name']} — {item['amount']} {group.currency}"
                for index, item in enumerate(group.top_categories, start=1)
            )
    lines.extend(
        [
            "",
            f"Без категории: {uncategorized}",
            f"Черновики: {draft_count}",
            f"Предстоящие регулярные: {upcoming}",
            f"Google Sheets: {sync_status}",
            f"Backup: {backup.status}",
        ]
    )
    return WeeklyReportResponse(
        workspace_id=workspace.id,
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        groups=groups,
        uncategorized_count=uncategorized,
        draft_count=draft_count,
        upcoming_recurring_count=upcoming,
        account_balances=[item.model_dump(mode="json") for item in balances],
        sync_status=sync_status,
        backup_status=backup.status,
        recipients=recipients,
        messages=split_telegram_message("\n".join(lines)),
    )


async def uncategorized_report(
    session: AsyncSession, workspace: Workspace
) -> UncategorizedReportResponse:
    items = list(
        (
            await session.scalars(
                select(FinancialTransaction)
                .where(
                    FinancialTransaction.workspace_id == workspace.id,
                    FinancialTransaction.transaction_type.in_(("income", "expense")),
                    FinancialTransaction.category_id.is_(None),
                    FinancialTransaction.status.in_(EFFECTIVE_STATUSES),
                    FinancialTransaction.deleted_at.is_(None),
                )
                .order_by(FinancialTransaction.occurred_at.desc())
            )
        ).all()
    )
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for item in items:
        totals[item.currency] += item.amount
    latest = [
        {
            "opaque_action": f"transaction:{item.id}",
            "occurred_at": item.occurred_at.isoformat(),
            "transaction_type": item.transaction_type,
            "amount": str(item.amount),
            "currency": item.currency,
        }
        for item in items[:10]
    ]
    recipients = await report_recipients(session, workspace.id, "uncategorized_reminder")
    lines = [f"Операции без категории: {len(items)}"]
    lines.extend(
        f"{currency}: {_format_money(amount)}" for currency, amount in sorted(totals.items())
    )
    lines.extend(
        f"{item['occurred_at'][:10]} — {item['amount']} {item['currency']}" for item in latest
    )
    return UncategorizedReportResponse(
        workspace_id=workspace.id,
        count=len(items),
        totals=[
            MoneyTotal(currency=key, amount=_money(value)) for key, value in sorted(totals.items())
        ],
        latest=latest,
        recipients=recipients,
        messages=split_telegram_message("\n".join(lines)),
    )


async def report_recipients(
    session: AsyncSession, workspace_id: uuid.UUID, event_type: str
) -> list[ReportRecipient]:
    rows = (
        await session.execute(
            select(
                NotificationSetting.user_id,
                TelegramLink.telegram_user_id,
                TelegramLink.telegram_chat_id,
            )
            .join(
                TelegramLink,
                (TelegramLink.user_id == NotificationSetting.user_id)
                & (TelegramLink.workspace_id == NotificationSetting.workspace_id),
            )
            .join(
                WorkspaceMember,
                (WorkspaceMember.user_id == NotificationSetting.user_id)
                & (WorkspaceMember.workspace_id == NotificationSetting.workspace_id),
            )
            .where(
                NotificationSetting.workspace_id == workspace_id,
                NotificationSetting.channel == "telegram",
                NotificationSetting.event_type == event_type,
                NotificationSetting.enabled.is_(True),
                TelegramLink.status == "active",
            )
        )
    ).all()
    return [
        ReportRecipient(
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
        )
        for user_id, telegram_user_id, chat_id in rows
    ]


async def _sync_status(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    binding = await session.scalar(
        select(GoogleSheetBinding).where(
            GoogleSheetBinding.workspace_id == workspace_id,
            GoogleSheetBinding.deleted_at.is_(None),
        )
    )
    if binding is None:
        return "not_configured"
    conflicts = len(
        list(
            (
                await session.scalars(
                    select(SyncConflict.id).where(
                        SyncConflict.workspace_id == workspace_id,
                        SyncConflict.status == "open",
                    )
                )
            ).all()
        )
    )
    failed = len(
        list(
            (
                await session.scalars(
                    select(SyncOutbox.id).where(
                        SyncOutbox.workspace_id == workspace_id,
                        SyncOutbox.status == "failed",
                    )
                )
            ).all()
        )
    )
    if conflicts or failed or binding.last_error_code:
        return "problem"
    if binding.status == "paused" or binding.sync_mode == "paused":
        return "paused"
    return "synchronized"


def split_telegram_message(value: str, limit: int = 3900) -> list[str]:
    if len(value) <= limit:
        return [value]
    chunks: list[str] = []
    current = ""
    for line in value.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def _format_money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")


def _format_signed(value: Decimal) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{_format_money(value)}"
