import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.transactions import FinancialTransaction, TransactionSplit
from app.services.financial_period_guard import period_bounds

EFFECTIVE_STATUSES = ("confirmed", "reconciled")
MONEY_QUANTUM = Decimal("0.0001")


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


@dataclass(slots=True)
class BudgetActual:
    income: Decimal = Decimal("0")
    expense: Decimal = Decimal("0")
    adjustment: Decimal = Decimal("0")
    category_expense: dict[uuid.UUID | None, Decimal] = field(
        default_factory=lambda: defaultdict(lambda: Decimal("0"))
    )

    @property
    def net_cashflow(self) -> Decimal:
        return money(self.income - self.expense + self.adjustment)


async def project_budget_actuals(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    timezone: str,
) -> dict[str, BudgetActual]:
    """Project exact-category budget actuals with Financial Report semantics."""
    start, end = period_bounds(period, timezone)
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction)
                .where(
                    FinancialTransaction.workspace_id == workspace_id,
                    FinancialTransaction.occurred_at >= start,
                    FinancialTransaction.occurred_at < end,
                    FinancialTransaction.status.in_(EFFECTIVE_STATUSES),
                    FinancialTransaction.deleted_at.is_(None),
                )
                .order_by(FinancialTransaction.occurred_at, FinancialTransaction.id)
            )
        ).all()
    )
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
                        FinancialTransaction.workspace_id == workspace_id,
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

    result: dict[str, BudgetActual] = defaultdict(BudgetActual)

    def allocate_expense(
        target: BudgetActual, original: FinancialTransaction, amount: Decimal
    ) -> None:
        transaction_splits = splits.get(original.id, [])
        if not transaction_splits:
            target.category_expense[original.category_id] += amount
            return
        remaining = money(amount)
        for index, split in enumerate(transaction_splits):
            allocated = (
                remaining
                if index == len(transaction_splits) - 1
                else money(amount * split.amount / original.amount)
            )
            target.category_expense[split.category_id] += allocated
            remaining = money(remaining - allocated)

    for item in transactions:
        target = result[item.currency]
        if item.transaction_type == "income":
            target.income += item.amount
        elif item.transaction_type == "expense":
            target.expense += item.amount
            allocate_expense(target, item, item.amount)
        elif item.transaction_type == "adjustment":
            target.adjustment += item.amount
        elif item.transaction_type == "refund":
            original = (
                originals.get(item.related_transaction_id)
                if item.related_transaction_id is not None
                else None
            )
            if original is not None and original.transaction_type == "expense":
                target.expense -= item.amount
                allocate_expense(target, original, -item.amount)
            elif original is not None and original.transaction_type == "income":
                target.income -= item.amount

    for target in result.values():
        target.income = money(target.income)
        target.expense = money(target.expense)
        target.adjustment = money(target.adjustment)
        target.category_expense = {
            category_id: money(amount) for category_id, amount in target.category_expense.items()
        }
    return dict(result)
