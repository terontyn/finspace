import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.accounts import Account


async def get_account(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> Account | None:
    statement = select(Account).where(
        Account.id == account_id, Account.workspace_id == workspace_id
    )
    if not include_deleted:
        statement = statement.where(Account.deleted_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def find_active_name(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> Account | None:
    statement = select(Account).where(
        Account.workspace_id == workspace_id,
        Account.name == name,
        Account.deleted_at.is_(None),
        Account.is_archived.is_(False),
    )
    if exclude_id is not None:
        statement = statement.where(Account.id != exclude_id)
    return await session.scalar(statement)


async def list_accounts(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    is_archived: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[Account], int]:
    filters = [Account.workspace_id == workspace_id, Account.deleted_at.is_(None)]
    if is_archived is not None:
        filters.append(Account.is_archived == is_archived)
    total = int(
        await session.scalar(select(func.count()).select_from(Account).where(*filters)) or 0
    )
    accounts = list(
        (
            await session.scalars(
                select(Account)
                .where(*filters)
                .order_by(Account.name, Account.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return accounts, total
