import hashlib
import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.accounts import Account
from app.db.models.transactions import FinancialTransaction, TransactionSplit


def canonical_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


def canonical_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def effective_financial_status(transaction: FinancialTransaction) -> str:
    if transaction.deleted_at is not None or transaction.status == "cancelled":
        return "excluded"
    if transaction.status in {"confirmed", "reconciled"}:
        return "effective"
    return transaction.status


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def hash_canonical(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


async def financial_state(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    cutoff_at: datetime,
) -> dict[str, Any]:
    accounts = list(
        (
            await session.scalars(
                select(Account).where(Account.workspace_id == workspace_id).order_by(Account.id)
            )
        ).all()
    )
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction)
                .where(
                    FinancialTransaction.workspace_id == workspace_id,
                    FinancialTransaction.occurred_at < cutoff_at,
                )
                .order_by(FinancialTransaction.id)
            )
        ).all()
    )
    transaction_ids = [item.id for item in transactions]
    split_rows = (
        list(
            (
                await session.scalars(
                    select(TransactionSplit)
                    .where(TransactionSplit.transaction_id.in_(transaction_ids))
                    .order_by(
                        TransactionSplit.transaction_id,
                        TransactionSplit.category_id,
                        TransactionSplit.amount,
                        TransactionSplit.id,
                    )
                )
            ).all()
        )
        if transaction_ids
        else []
    )
    splits: dict[uuid.UUID, list[dict[str, str]]] = defaultdict(list)
    for item in split_rows:
        splits[item.transaction_id].append(
            {
                "category_id": str(item.category_id),
                "amount": canonical_decimal(item.amount),
            }
        )

    return {
        "workspace_id": str(workspace_id),
        "cutoff_at": canonical_datetime(cutoff_at),
        "accounts": [
            {
                "id": str(item.id),
                "currency": item.currency,
                "opening_balance": canonical_decimal(item.opening_balance),
                "opening_balance_at": canonical_datetime(item.opening_balance_at),
                "is_deleted": item.deleted_at is not None,
            }
            for item in accounts
            if item.opening_balance_at < cutoff_at
        ],
        "transactions": [
            {
                "id": str(item.id),
                "status": effective_financial_status(item),
                "occurred_at": canonical_datetime(item.occurred_at),
                "transaction_type": item.transaction_type,
                "amount": canonical_decimal(item.amount),
                "currency": item.currency,
                "account_id": str(item.account_id),
                "target_account_id": (
                    str(item.target_account_id) if item.target_account_id is not None else None
                ),
                "category_id": str(item.category_id) if item.category_id is not None else None,
                "related_transaction_id": (
                    str(item.related_transaction_id)
                    if item.related_transaction_id is not None
                    else None
                ),
                "splits": splits.get(item.id, []),
            }
            for item in transactions
        ],
    }


async def financial_fingerprint(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    cutoff_at: datetime,
) -> str:
    return hash_canonical(await financial_state(session, workspace_id, cutoff_at))
