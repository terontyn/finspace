import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.categories import Category
from app.dependencies.context import RequestContext
from app.repositories import categories as repository
from app.schemas.categories import (
    CategoryCreate,
    CategoryResponse,
    CategoryTreeItem,
    CategoryUpdate,
)
from app.services.audit import record_audit, snapshot
from app.services.sync_outbox import enqueue_entity


async def _validate_parent(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    *,
    category_id: uuid.UUID | None = None,
) -> None:
    if parent_id is None:
        return
    if category_id == parent_id:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Category cannot be its own parent",
        )
    parent = await repository.get_category(session, workspace_id, parent_id)
    if parent is None:
        raise ApiError(
            status_code=404, code="CATEGORY_NOT_FOUND", message="Parent category was not found"
        )
    visited: set[uuid.UUID] = set()
    current = parent
    while current.parent_id is not None:
        if current.id in visited or current.parent_id == category_id:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="Category hierarchy cannot contain a cycle",
            )
        visited.add(current.id)
        ancestor = await repository.get_category(session, workspace_id, current.parent_id)
        if ancestor is None:
            break
        current = ancestor


async def create_category(
    session: AsyncSession,
    context: RequestContext,
    data: CategoryCreate,
    *,
    commit: bool = True,
) -> Category:
    await _validate_parent(session, context.workspace.id, data.parent_id)
    if await repository.find_level_name(session, context.workspace.id, data.parent_id, data.name):
        raise ApiError(status_code=409, code="DUPLICATE_NAME", message="Category exists")
    category = Category(workspace_id=context.workspace.id, **data.model_dump())
    session.add(category)
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="category",
        entity_id=category.id,
        action="create",
        before_data=None,
        after_data=snapshot("category", category),
        request_id=context.request_id,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="category",
        entity=category,
    )
    if commit:
        await session.commit()
    return category


async def update_category(
    session: AsyncSession,
    context: RequestContext,
    category_id: uuid.UUID,
    data: CategoryUpdate,
    *,
    commit: bool = True,
    audit_source: str = "api",
) -> Category:
    category = await repository.get_category(session, context.workspace.id, category_id)
    if category is None:
        raise ApiError(status_code=404, code="CATEGORY_NOT_FOUND", message="Category was not found")
    if category.version != data.version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    next_parent = changes.get("parent_id", category.parent_id)
    next_name = str(changes.get("name", category.name))
    await _validate_parent(
        session,
        context.workspace.id,
        next_parent,
        category_id=category.id,
    )
    if await repository.find_level_name(
        session,
        context.workspace.id,
        next_parent,
        next_name,
        exclude_id=category.id,
    ):
        raise ApiError(status_code=409, code="DUPLICATE_NAME", message="Category exists")
    before = snapshot("category", category)
    was_archived = category.is_archived
    for field, value in changes.items():
        setattr(category, field, value)
    category.version += 1
    category.updated_at = datetime.now(UTC)
    await session.flush()
    action = "archive" if not was_archived and category.is_archived else "update"
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="category",
        entity_id=category.id,
        action=action,
        before_data=before,
        after_data=snapshot("category", category),
        request_id=context.request_id,
        source=audit_source,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="category",
        entity=category,
        operation="archive" if action == "archive" else "upsert",
    )
    if commit:
        await session.commit()
    return category


async def delete_category(
    session: AsyncSession,
    context: RequestContext,
    category_id: uuid.UUID,
    version: int,
    *,
    commit: bool = True,
) -> Category:
    category = await repository.get_category(session, context.workspace.id, category_id)
    if category is None:
        raise ApiError(status_code=404, code="CATEGORY_NOT_FOUND", message="Category was not found")
    if category.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    before = snapshot("category", category)
    changed_at = datetime.now(UTC)
    category.deleted_at = changed_at
    category.updated_at = changed_at
    category.version += 1
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="category",
        entity_id=category.id,
        action="delete",
        before_data=before,
        after_data=snapshot("category", category),
        request_id=context.request_id,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="category",
        entity=category,
        operation="delete",
    )
    if commit:
        await session.commit()
    return category


async def restore_category(
    session: AsyncSession,
    context: RequestContext,
    category_id: uuid.UUID,
    version: int,
    *,
    commit: bool = True,
) -> Category:
    category = await repository.get_category(
        session, context.workspace.id, category_id, include_deleted=True
    )
    if category is None:
        raise ApiError(status_code=404, code="CATEGORY_NOT_FOUND", message="Category was not found")
    if category.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    if category.deleted_at is None:
        return category
    if await repository.find_level_name(
        session,
        context.workspace.id,
        category.parent_id,
        category.name,
        exclude_id=category.id,
    ):
        raise ApiError(status_code=409, code="DUPLICATE_NAME", message="Name is in use")
    before = snapshot("category", category)
    category.deleted_at = None
    category.updated_at = datetime.now(UTC)
    category.version += 1
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="category",
        entity_id=category.id,
        action="restore",
        before_data=before,
        after_data=snapshot("category", category),
        request_id=context.request_id,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="category",
        entity=category,
        operation="restore",
    )
    if commit:
        await session.commit()
    return category


def build_tree(categories: list[Category]) -> list[CategoryTreeItem]:
    nodes = {
        item.id: CategoryTreeItem(**CategoryResponse.model_validate(item).model_dump(), children=[])
        for item in categories
    }
    roots: list[CategoryTreeItem] = []
    for item in categories:
        node = nodes[item.id]
        if item.parent_id is not None and item.parent_id in nodes:
            nodes[item.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots
