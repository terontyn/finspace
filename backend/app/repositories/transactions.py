import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.transactions import FinancialTransaction, TransactionSplit


async def get_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> FinancialTransaction | None:
    statement = select(FinancialTransaction).where(
        FinancialTransaction.id == transaction_id,
        FinancialTransaction.workspace_id == workspace_id,
    )
    if not include_deleted:
        statement = statement.where(FinancialTransaction.deleted_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def get_splits(session: AsyncSession, transaction_id: uuid.UUID) -> list[TransactionSplit]:
    return list(
        (
            await session.scalars(
                select(TransactionSplit)
                .where(TransactionSplit.transaction_id == transaction_id)
                .order_by(TransactionSplit.created_at, TransactionSplit.id)
            )
        ).all()
    )


async def list_transactions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    transaction_type: str | None,
    status: str | None,
    account_id: uuid.UUID | None,
    category_id: uuid.UUID | None,
    payee_id: uuid.UUID | None,
    search: str | None,
    limit: int,
    offset: int,
) -> tuple[list[FinancialTransaction], int]:
    filters = [
        FinancialTransaction.workspace_id == workspace_id,
        FinancialTransaction.deleted_at.is_(None),
    ]
    if date_from is not None:
        filters.append(FinancialTransaction.occurred_at >= date_from)
    if date_to is not None:
        filters.append(FinancialTransaction.occurred_at <= date_to)
    if transaction_type is not None:
        filters.append(FinancialTransaction.transaction_type == transaction_type)
    if status is not None:
        filters.append(FinancialTransaction.status == status)
    if account_id is not None:
        filters.append(
            or_(
                FinancialTransaction.account_id == account_id,
                FinancialTransaction.target_account_id == account_id,
            )
        )
    if category_id is not None:
        filters.append(FinancialTransaction.category_id == category_id)
    if payee_id is not None:
        filters.append(FinancialTransaction.payee_id == payee_id)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                FinancialTransaction.counterparty.ilike(pattern),
                FinancialTransaction.description.ilike(pattern),
            )
        )
    total = int(
        await session.scalar(select(func.count()).select_from(FinancialTransaction).where(*filters))
        or 0
    )
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction)
                .where(*filters)
                .order_by(FinancialTransaction.occurred_at.desc(), FinancialTransaction.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return transactions, total
