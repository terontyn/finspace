import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.automations import NotificationSetting, RecurringRule, TelegramLink
from app.db.models.categories import Category
from app.db.models.google_sync import GoogleSheetBinding, SyncConflict, SyncOutbox
from app.db.models.transactions import FinancialTransaction, TransactionSplit
from app.db.models.users import Workspace, WorkspaceMember
from app.schemas.automations import (
    MoneyTotal,
    ReportRecipient,
    UncategorizedReportResponse,
    WeeklyReportGroup,
    WeeklyReportResponse,
)
from app.schemas.financial_reports import (
    FinancialReportCategory,
    FinancialReportExpense,
    FinancialReportGroup,
    FinancialReportMonth,
    FinancialReportPeriod,
    FinancialReportResponse,
)
from app.services.backup_status import get_backup_status
from app.services.calculations import calculate_balances

EFFECTIVE_STATUSES = ("confirmed", "reconciled")


def _previous_month_start(value: date) -> date:
    if value.month == 1:
        return date(value.year - 1, 12, 1)
    return date(value.year, value.month - 1, 1)


def _month_keys(start: date, end: date) -> list[str]:
    current = start.replace(day=1)
    last = end.replace(day=1)
    result: list[str] = []
    while current <= last:
        result.append(current.strftime("%Y-%m"))
        current = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
    return result


async def financial_report(
    session: AsyncSession,
    workspace: Workspace,
    *,
    date_from: date,
    date_to: date,
    currency: str | None,
) -> FinancialReportResponse:
    if date_from > date_to:
        raise ApiError(
            status_code=422,
            code="INVALID_REPORT_PERIOD",
            message="date_from must not be after date_to",
        )

    zone = ZoneInfo(workspace.timezone)
    cutoff_from = datetime.combine(date_from, time.min, tzinfo=zone).astimezone(UTC)
    cutoff_to = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    comparison_date_from = _previous_month_start(date_from.replace(day=1))
    comparison_cutoff = datetime.combine(comparison_date_from, time.min, tzinfo=zone).astimezone(
        UTC
    )
    filters = [
        FinancialTransaction.workspace_id == workspace.id,
        FinancialTransaction.occurred_at >= comparison_cutoff,
        FinancialTransaction.occurred_at < cutoff_to,
        FinancialTransaction.status.in_(EFFECTIVE_STATUSES),
        FinancialTransaction.deleted_at.is_(None),
    ]
    if currency is not None:
        filters.append(FinancialTransaction.currency == currency)
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction)
                .where(*filters)
                .order_by(FinancialTransaction.occurred_at, FinancialTransaction.id)
            )
        ).all()
    )
    selected = [item for item in transactions if item.occurred_at >= cutoff_from]

    related_ids = {
        item.related_transaction_id
        for item in transactions
        if item.transaction_type == "refund" and item.related_transaction_id is not None
    }
    originals = {item.id: item for item in transactions}
    if related_ids:
        related = list(
            (
                await session.scalars(
                    select(FinancialTransaction).where(
                        FinancialTransaction.workspace_id == workspace.id,
                        FinancialTransaction.id.in_(related_ids),
                    )
                )
            ).all()
        )
        originals.update({item.id: item for item in related})

    expense_originals = {
        item.id: item for item in originals.values() if item.transaction_type == "expense"
    }
    split_rows = (
        list(
            (
                await session.scalars(
                    select(TransactionSplit)
                    .where(TransactionSplit.transaction_id.in_(expense_originals))
                    .order_by(TransactionSplit.transaction_id, TransactionSplit.id)
                )
            ).all()
        )
        if expense_originals
        else []
    )
    splits: dict[uuid.UUID, list[TransactionSplit]] = defaultdict(list)
    for split in split_rows:
        splits[split.transaction_id].append(split)

    category_ids = {
        item.category_id for item in expense_originals.values() if item.category_id is not None
    } | {item.category_id for item in split_rows}
    category_names = {
        item.id: item.name
        for item in (
            list(
                (
                    await session.scalars(
                        select(Category).where(
                            Category.workspace_id == workspace.id,
                            Category.id.in_(category_ids),
                        )
                    )
                ).all()
            )
            if category_ids
            else []
        )
    }
    account_ids = {item.account_id for item in selected if item.transaction_type == "expense"}
    account_names = {
        item.id: item.name
        for item in (
            list(
                (
                    await session.scalars(
                        select(Account).where(
                            Account.workspace_id == workspace.id,
                            Account.id.in_(account_ids),
                        )
                    )
                ).all()
            )
            if account_ids
            else []
        )
    }

    def category_name(category_id: uuid.UUID | None) -> str:
        if category_id is None:
            return "Без категории"
        return category_names.get(category_id, "Без категории")

    def empty_totals() -> dict[str, Decimal | int]:
        return {
            "income": Decimal("0"),
            "expense": Decimal("0"),
            "adjustment": Decimal("0"),
            "transfer": Decimal("0"),
            "count": 0,
        }

    def apply_effect(target: dict[str, Decimal | int], item: FinancialTransaction) -> None:
        target["count"] = int(target["count"]) + 1
        if item.transaction_type == "income":
            target["income"] = Decimal(target["income"]) + item.amount
        elif item.transaction_type == "expense":
            target["expense"] = Decimal(target["expense"]) + item.amount
        elif item.transaction_type == "adjustment":
            target["adjustment"] = Decimal(target["adjustment"]) + item.amount
        elif item.transaction_type == "transfer":
            target["transfer"] = Decimal(target["transfer"]) + item.amount
        elif item.transaction_type == "refund":
            original = (
                originals.get(item.related_transaction_id)
                if item.related_transaction_id is not None
                else None
            )
            if original is not None and original.transaction_type == "expense":
                target["expense"] = Decimal(target["expense"]) - item.amount
            elif original is not None and original.transaction_type == "income":
                target["income"] = Decimal(target["income"]) - item.amount

    totals: dict[str, dict[str, Decimal | int]] = defaultdict(empty_totals)
    monthly: dict[str, dict[str, dict[str, Decimal | int]]] = defaultdict(
        lambda: defaultdict(empty_totals)
    )
    for item in transactions:
        month = item.occurred_at.astimezone(zone).strftime("%Y-%m")
        apply_effect(monthly[item.currency][month], item)
    for item in selected:
        apply_effect(totals[item.currency], item)

    category_amounts: dict[str, dict[uuid.UUID | None, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    category_counts: dict[str, dict[uuid.UUID | None, int]] = defaultdict(lambda: defaultdict(int))

    def allocate_expense(
        currency_code: str, original: FinancialTransaction, amount: Decimal
    ) -> None:
        transaction_splits = splits.get(original.id, [])
        if not transaction_splits:
            category_amounts[currency_code][original.category_id] += amount
            category_counts[currency_code][original.category_id] += 1
            return
        remaining = _money(amount)
        for index, split in enumerate(transaction_splits):
            allocated = (
                remaining
                if index == len(transaction_splits) - 1
                else _money(amount * split.amount / original.amount)
            )
            category_amounts[currency_code][split.category_id] += allocated
            category_counts[currency_code][split.category_id] += 1
            remaining = _money(remaining - allocated)

    for item in selected:
        if item.transaction_type == "expense":
            allocate_expense(item.currency, item, item.amount)
        elif item.transaction_type == "refund":
            original = (
                originals.get(item.related_transaction_id)
                if item.related_transaction_id is not None
                else None
            )
            if original is not None and original.transaction_type == "expense":
                allocate_expense(item.currency, original, -item.amount)

    month_keys = _month_keys(comparison_date_from, date_to)
    currencies = sorted(set(totals) | set(monthly))
    groups: list[FinancialReportGroup] = []
    for currency_code in currencies:
        values = totals[currency_code]
        income = _money(Decimal(values["income"]))
        expense = _money(Decimal(values["expense"]))
        adjustment = _money(Decimal(values["adjustment"]))
        categories = [
            FinancialReportCategory(
                category_id=category_id,
                name=category_name(category_id),
                amount=_money(amount),
                transaction_count=category_counts[currency_code][category_id],
            )
            for category_id, amount in sorted(
                category_amounts[currency_code].items(),
                key=lambda item: (-item[1], category_name(item[0])),
            )
        ]
        month_items: list[FinancialReportMonth] = []
        for month in month_keys:
            month_values = monthly[currency_code][month]
            month_income = _money(Decimal(month_values["income"]))
            month_expense = _money(Decimal(month_values["expense"]))
            month_adjustment = _money(Decimal(month_values["adjustment"]))
            month_items.append(
                FinancialReportMonth(
                    month=month,
                    income=month_income,
                    expense=month_expense,
                    adjustment=month_adjustment,
                    net_cashflow=_money(month_income - month_expense + month_adjustment),
                    transactions_count=int(month_values["count"]),
                )
            )
        largest = sorted(
            (
                item
                for item in selected
                if item.currency == currency_code and item.transaction_type == "expense"
            ),
            key=lambda item: (item.amount, item.occurred_at, item.id),
            reverse=True,
        )[:10]
        groups.append(
            FinancialReportGroup(
                currency=currency_code,
                income=income,
                expense=expense,
                adjustment=adjustment,
                net_cashflow=_money(income - expense + adjustment),
                transfer_volume=_money(Decimal(values["transfer"])),
                transactions_count=int(values["count"]),
                spending_by_category=categories,
                monthly_comparison=month_items,
                largest_expenses=[
                    FinancialReportExpense(
                        transaction_id=item.id,
                        occurred_at=item.occurred_at,
                        amount=item.amount,
                        account_id=item.account_id,
                        account_name=account_names.get(item.account_id, "Счёт"),
                        category_name=(
                            "Разделено" if splits.get(item.id) else category_name(item.category_id)
                        ),
                        counterparty=item.counterparty,
                        description=item.description,
                    )
                    for item in largest
                ],
            )
        )

    return FinancialReportResponse(
        period=FinancialReportPeriod(
            date_from=date_from,
            date_to=date_to,
            cutoff_from=cutoff_from,
            cutoff_to=cutoff_to,
            timezone=workspace.timezone,
        ),
        groups=groups,
    )


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
