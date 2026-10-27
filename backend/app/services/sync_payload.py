import uuid
from datetime import UTC
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.transactions import FinancialTransaction
from app.services.sync_hash import canonical_decimal, row_hash

TRANSACTION_TYPE_LABELS = {
    "income": "Доход",
    "expense": "Расход",
    "transfer": "Перевод",
    "refund": "Возврат",
    "adjustment": "Корректировка",
}
TRANSACTION_STATUS_LABELS = {
    "draft": "Черновик",
    "confirmed": "Подтверждена",
    "reconciled": "Сверена",
    "cancelled": "Отменена",
}
SOURCE_LABELS = {
    "manual": "Вручную",
    "api": "Приложение",
    "import": "Импорт",
    "google_sheets": "Google Sheets",
    "system": "Система",
}


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def transaction_payload(transaction: FinancialTransaction) -> dict[str, Any]:
    return {
        "entity_id": transaction.id,
        "version": transaction.version,
        "occurred_at": transaction.occurred_at.astimezone(UTC),
        "transaction_type": transaction.transaction_type,
        "amount": transaction.amount,
        "currency": transaction.currency,
        "account_id": transaction.account_id,
        "target_account_id": transaction.target_account_id,
        "category_id": transaction.category_id,
        "counterparty": transaction.counterparty,
        "description": transaction.description,
        "comment": transaction.comment,
        "status": transaction.status,
        "deleted_at": transaction.deleted_at,
    }


def account_payload(account: Account) -> dict[str, Any]:
    return {
        "entity_id": account.id,
        "version": account.version,
        "name": account.name,
        "account_type": account.account_type,
        "currency": account.currency,
        "institution": account.institution,
        "opening_balance": account.opening_balance,
        "opening_balance_at": account.opening_balance_at,
        "credit_limit": account.credit_limit,
        "is_archived": account.is_archived,
        "deleted_at": account.deleted_at,
    }


def category_payload(category: Category) -> dict[str, Any]:
    return {
        "entity_id": category.id,
        "version": category.version,
        "parent_id": category.parent_id,
        "name": category.name,
        "category_type": category.category_type,
        "icon": category.icon,
        "color": category.color,
        "sort_order": category.sort_order,
        "is_archived": category.is_archived,
        "deleted_at": category.deleted_at,
    }


def entity_payload(entity_type: str, entity: object) -> dict[str, Any]:
    if entity_type == "transaction" and isinstance(entity, FinancialTransaction):
        return transaction_payload(entity)
    if entity_type == "account" and isinstance(entity, Account):
        return account_payload(entity)
    if entity_type == "category" and isinstance(entity, Category):
        return category_payload(entity)
    raise ValueError(f"Unsupported synchronized entity: {entity_type}")


def transaction_row(
    transaction: FinancialTransaction,
    *,
    timezone_name: str,
    account_names: dict[uuid.UUID, str],
    category_names: dict[uuid.UUID, str],
    owner_name: str | None,
) -> list[Any]:
    occurred = transaction.occurred_at.astimezone(ZoneInfo(timezone_name))
    category_name = (
        category_names.get(transaction.category_id, "")
        if transaction.category_id is not None
        else ""
    )
    payload = transaction_payload(transaction)
    return [
        occurred.date().isoformat(),
        occurred.strftime("%H:%M:%S"),
        TRANSACTION_TYPE_LABELS.get(transaction.transaction_type, transaction.transaction_type),
        canonical_decimal(transaction.amount),
        transaction.currency,
        account_names.get(transaction.account_id, ""),
        (
            account_names.get(transaction.target_account_id, "")
            if transaction.target_account_id is not None
            else ""
        ),
        category_name,
        "",
        transaction.counterparty or "",
        transaction.description or "",
        transaction.comment or "",
        TRANSACTION_STATUS_LABELS.get(transaction.status, transaction.status),
        SOURCE_LABELS.get(transaction.source, transaction.source),
        owner_name or "",
        _iso(transaction.updated_at),
        "DELETED" if transaction.deleted_at else "SYNCED",
        "",
        str(transaction.id),
        str(transaction.workspace_id),
        str(transaction.account_id),
        str(transaction.target_account_id or ""),
        str(transaction.category_id or ""),
        transaction.version,
        row_hash(payload),
        _iso(transaction.updated_at),
        _iso(transaction.deleted_at) or "",
    ]


def account_row(account: Account, *, calculated_balance: Decimal | None) -> list[Any]:
    payload = account_payload(account)
    return [
        account.name,
        account.account_type,
        account.currency,
        account.institution or "",
        canonical_decimal(account.opening_balance),
        _iso(account.opening_balance_at),
        canonical_decimal(account.credit_limit) if account.credit_limit is not None else "",
        "Да" if account.is_archived else "Нет",
        canonical_decimal(calculated_balance) if calculated_balance is not None else "",
        _iso(account.updated_at),
        "DELETED" if account.deleted_at else "SYNCED",
        "",
        str(account.id),
        account.version,
        row_hash(payload),
        _iso(account.updated_at),
        _iso(account.deleted_at) or "",
    ]


def category_row(category: Category, *, parent_name: str | None) -> list[Any]:
    payload = category_payload(category)
    return [
        category.name,
        category.category_type,
        parent_name or "",
        category.icon or "",
        category.color or "",
        category.sort_order,
        "Да" if category.is_archived else "Нет",
        _iso(category.updated_at),
        "DELETED" if category.deleted_at else "SYNCED",
        "",
        str(category.id),
        str(category.parent_id or ""),
        category.version,
        row_hash(payload),
        _iso(category.updated_at),
        _iso(category.deleted_at) or "",
    ]
