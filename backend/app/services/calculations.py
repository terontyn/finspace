import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.accounts import Account
from app.db.models.transactions import FinancialTransaction
from app.schemas.accounts import AccountBalance
from app.schemas.transactions import FinancialSummaryGroup, FinancialSummaryResponse

EFFECTIVE_STATUSES = {"confirmed", "reconciled"}


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


async def calculate_balances(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[AccountBalance]:
    accounts = list(
        (
            await session.scalars(
                select(Account)
                .where(Account.workspace_id == workspace_id, Account.deleted_at.is_(None))
                .order_by(Account.name, Account.id)
            )
        ).all()
    )
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace_id,
                    FinancialTransaction.status.in_(EFFECTIVE_STATUSES),
                    FinancialTransaction.deleted_at.is_(None),
                )
            )
        ).all()
    )
    originals = {item.id: item for item in transactions}
    result: list[AccountBalance] = []
    for account in accounts:
        balance = account.opening_balance
        for item in transactions:
            if item.occurred_at < account.opening_balance_at:
                continue
            if item.transaction_type == "income" and item.account_id == account.id:
                balance += item.amount
            elif item.transaction_type == "expense" and item.account_id == account.id:
                balance -= item.amount
            elif item.transaction_type == "transfer":
                if item.account_id == account.id:
                    balance -= item.amount
                if item.target_account_id == account.id:
                    balance += item.amount
            elif item.transaction_type == "adjustment" and item.account_id == account.id:
                balance += item.amount
            elif item.transaction_type == "refund" and item.account_id == account.id:
                original = (
                    originals.get(item.related_transaction_id)
                    if item.related_transaction_id is not None
                    else None
                )
                if original is None:
                    original = await session.get(FinancialTransaction, item.related_transaction_id)
                if original is not None and original.transaction_type == "expense":
                    balance += item.amount
                elif original is not None and original.transaction_type == "income":
                    balance -= item.amount
        result.append(
            AccountBalance(
                account_id=account.id,
                name=account.name,
                currency=account.currency,
                opening_balance=account.opening_balance,
                balance=_money(balance),
            )
        )
    return result


async def calculate_summary(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> FinancialSummaryResponse:
    filters = [
        FinancialTransaction.workspace_id == workspace_id,
        FinancialTransaction.status.in_(EFFECTIVE_STATUSES),
        FinancialTransaction.deleted_at.is_(None),
    ]
    if date_from is not None:
        filters.append(FinancialTransaction.occurred_at >= date_from)
    if date_to is not None:
        filters.append(FinancialTransaction.occurred_at <= date_to)
    transactions = list((await session.scalars(select(FinancialTransaction).where(*filters))).all())
    originals = {item.id: item for item in transactions}
    totals: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "income": Decimal("0"),
            "expense": Decimal("0"),
            "adjustment": Decimal("0"),
            "transfer": Decimal("0"),
            "count": 0,
        }
    )
    for item in transactions:
        group = totals[item.currency]
        group["count"] = int(group["count"]) + 1
        if item.transaction_type == "income":
            group["income"] = Decimal(group["income"]) + item.amount
        elif item.transaction_type == "expense":
            group["expense"] = Decimal(group["expense"]) + item.amount
        elif item.transaction_type == "transfer":
            group["transfer"] = Decimal(group["transfer"]) + item.amount
        elif item.transaction_type == "adjustment":
            group["adjustment"] = Decimal(group["adjustment"]) + item.amount
        elif item.transaction_type == "refund":
            original = (
                originals.get(item.related_transaction_id)
                if item.related_transaction_id is not None
                else None
            )
            if original is None:
                original = await session.scalar(
                    select(FinancialTransaction).where(
                        FinancialTransaction.id == item.related_transaction_id,
                        or_(
                            FinancialTransaction.transaction_type == "income",
                            FinancialTransaction.transaction_type == "expense",
                        ),
                    )
                )
            if original is not None and original.transaction_type == "expense":
                group["expense"] = Decimal(group["expense"]) - item.amount
            elif original is not None and original.transaction_type == "income":
                group["income"] = Decimal(group["income"]) - item.amount

    groups = []
    for currency in sorted(totals):
        values = totals[currency]
        income = Decimal(values["income"])
        expense = Decimal(values["expense"])
        adjustment = Decimal(values["adjustment"])
        groups.append(
            FinancialSummaryGroup(
                currency=currency,
                income=_money(income),
                expense=_money(expense),
                net_cashflow=_money(income - expense + adjustment),
                transfer_volume=_money(Decimal(values["transfer"])),
                transactions_count=int(values["count"]),
            )
        )
    return FinancialSummaryResponse(groups=groups)
