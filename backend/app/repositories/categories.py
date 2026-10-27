import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.categories import Category


async def get_category(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    category_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> Category | None:
    statement = select(Category).where(
        Category.id == category_id, Category.workspace_id == workspace_id
    )
    if not include_deleted:
        statement = statement.where(Category.deleted_at.is_(None))
    return await session.scalar(statement)


async def find_level_name(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> Category | None:
    statement = select(Category).where(
        Category.workspace_id == workspace_id,
        Category.parent_id == parent_id,
        Category.name == name,
        Category.deleted_at.is_(None),
    )
    if exclude_id is not None:
        statement = statement.where(Category.id != exclude_id)
    return await session.scalar(statement)


async def list_categories(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    category_type: str | None,
    parent_id: uuid.UUID | None,
    filter_parent: bool,
    is_archived: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[Category], int]:
    filters = [Category.workspace_id == workspace_id, Category.deleted_at.is_(None)]
    if category_type is not None:
        filters.append(Category.category_type == category_type)
    if filter_parent:
        filters.append(Category.parent_id == parent_id)
    if is_archived is not None:
        filters.append(Category.is_archived == is_archived)
    total = int(
        await session.scalar(select(func.count()).select_from(Category).where(*filters)) or 0
    )
    categories = list(
        (
            await session.scalars(
                select(Category)
                .where(*filters)
                .order_by(Category.sort_order, Category.name, Category.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return categories, total
