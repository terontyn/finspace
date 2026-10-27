import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account_reconciliation import (
    AccountReconciliation,
    AccountReconciliationItem,
)


async def get_reconciliation(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
) -> AccountReconciliation | None:
    return await session.scalar(
        select(AccountReconciliation).where(
            AccountReconciliation.id == reconciliation_id,
            AccountReconciliation.workspace_id == workspace_id,
            AccountReconciliation.account_id == account_id,
        )
    )


async def get_by_idempotency_key(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    idempotency_key: str,
) -> AccountReconciliation | None:
    return await session.scalar(
        select(AccountReconciliation).where(
            AccountReconciliation.workspace_id == workspace_id,
            AccountReconciliation.idempotency_key == idempotency_key,
        )
    )


async def list_reconciliations(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[AccountReconciliation], int]:
    filters = [
        AccountReconciliation.workspace_id == workspace_id,
        AccountReconciliation.account_id == account_id,
    ]
    total = int(
        await session.scalar(
            select(func.count()).select_from(AccountReconciliation).where(*filters)
        )
        or 0
    )
    items = list(
        (
            await session.scalars(
                select(AccountReconciliation)
                .where(*filters)
                .order_by(
                    AccountReconciliation.statement_date.desc(),
                    AccountReconciliation.confirmed_at.desc(),
                    AccountReconciliation.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def transaction_ids(session: AsyncSession, reconciliation_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await session.scalars(
                select(AccountReconciliationItem.transaction_id)
                .where(AccountReconciliationItem.reconciliation_id == reconciliation_id)
                .order_by(AccountReconciliationItem.transaction_id)
            )
        ).all()
    )


async def linked_accounts_by_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction_ids: list[uuid.UUID],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    if not transaction_ids:
        return {}
    rows = (
        await session.execute(
            select(
                AccountReconciliationItem.transaction_id,
                AccountReconciliation.account_id,
            )
            .join(
                AccountReconciliation,
                AccountReconciliation.id == AccountReconciliationItem.reconciliation_id,
            )
            .where(
                AccountReconciliation.workspace_id == workspace_id,
                AccountReconciliation.status == "confirmed",
                AccountReconciliationItem.transaction_id.in_(transaction_ids),
            )
        )
    ).all()
    result: dict[uuid.UUID, set[uuid.UUID]] = {}
    for transaction_id, account_id in rows:
        result.setdefault(transaction_id, set()).add(account_id)
    return result
