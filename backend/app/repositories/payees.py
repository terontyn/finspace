import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.payees import Payee, PayeeAlias


async def get_payee(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    payee_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
    for_share: bool = False,
    include_aliases: bool = False,
) -> Payee | None:
    if for_update and for_share:
        raise ValueError("Payee row cannot use FOR UPDATE and FOR SHARE together")
    statement = select(Payee).where(Payee.id == payee_id, Payee.workspace_id == workspace_id)
    if not include_deleted:
        statement = statement.where(Payee.deleted_at.is_(None))
    if include_aliases:
        statement = statement.options(selectinload(Payee.aliases)).execution_options(
            populate_existing=True
        )
    if for_update:
        statement = statement.with_for_update()
    elif for_share:
        statement = statement.with_for_update(read=True)
    return await session.scalar(statement)


async def list_payees(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    search: str | None,
    include_deleted: bool,
    limit: int,
    offset: int,
) -> tuple[list[Payee], int]:
    filters = [Payee.workspace_id == workspace_id]
    if not include_deleted:
        filters.append(Payee.deleted_at.is_(None))
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(or_(Payee.name.ilike(pattern), Payee.notes.ilike(pattern)))
    total = int(await session.scalar(select(func.count()).select_from(Payee).where(*filters)) or 0)
    items = list(
        (
            await session.scalars(
                select(Payee)
                .options(selectinload(Payee.aliases))
                .where(*filters)
                .order_by(Payee.name, Payee.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def get_alias(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    payee_id: uuid.UUID,
    alias_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> PayeeAlias | None:
    statement = select(PayeeAlias).where(
        PayeeAlias.id == alias_id,
        PayeeAlias.workspace_id == workspace_id,
        PayeeAlias.payee_id == payee_id,
    )
    if not include_deleted:
        statement = statement.where(PayeeAlias.deleted_at.is_(None))
    return await session.scalar(statement)


async def find_alias_candidate(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    normalized_alias_hash: str,
) -> PayeeAlias | None:
    return await session.scalar(
        select(PayeeAlias).where(
            PayeeAlias.workspace_id == workspace_id,
            PayeeAlias.normalized_alias_hash == normalized_alias_hash,
        )
    )


async def get_payees_by_ids(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    payee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, Payee]:
    if not payee_ids:
        return {}
    payees = list(
        (
            await session.scalars(
                select(Payee).where(
                    Payee.workspace_id == workspace_id,
                    Payee.id.in_(payee_ids),
                )
            )
        ).all()
    )
    return {payee.id: payee for payee in payees}
