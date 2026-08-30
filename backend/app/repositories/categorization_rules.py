import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.categorization_rules import CategorizationRule


async def get_rule(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> CategorizationRule | None:
    statement = select(CategorizationRule).where(
        CategorizationRule.id == rule_id,
        CategorizationRule.workspace_id == workspace_id,
    )
    if not include_deleted:
        statement = statement.where(CategorizationRule.deleted_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def list_rules(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    include_deleted: bool,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[CategorizationRule], int]:
    filters = [CategorizationRule.workspace_id == workspace_id]
    if not include_deleted:
        filters.append(CategorizationRule.deleted_at.is_(None))
    if is_active is not None:
        filters.append(CategorizationRule.is_active == is_active)
    total = int(
        await session.scalar(select(func.count()).select_from(CategorizationRule).where(*filters))
        or 0
    )
    items = list(
        (
            await session.scalars(
                select(CategorizationRule)
                .where(*filters)
                .order_by(
                    CategorizationRule.priority,
                    CategorizationRule.created_at,
                    CategorizationRule.id,
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def active_rules(
    session: AsyncSession,
    workspace_id: uuid.UUID,
) -> list[CategorizationRule]:
    return list(
        (
            await session.scalars(
                select(CategorizationRule)
                .where(
                    CategorizationRule.workspace_id == workspace_id,
                    CategorizationRule.deleted_at.is_(None),
                    CategorizationRule.is_active.is_(True),
                )
                .order_by(
                    CategorizationRule.priority,
                    CategorizationRule.created_at,
                    CategorizationRule.id,
                )
            )
        ).all()
    )
