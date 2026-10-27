import uuid

from fastapi import APIRouter, Query

from app.core.errors import ApiError
from app.dependencies.context import CurrentContext, EditorContext
from app.dependencies.database import DbSession
from app.repositories import categories as repository
from app.schemas.accounts import VersionRequest
from app.schemas.categories import (
    CategoryCreate,
    CategoryPage,
    CategoryResponse,
    CategoryTreeItem,
    CategoryType,
    CategoryUpdate,
)
from app.schemas.common import PageMeta
from app.services import categories as service

router = APIRouter()


async def _commit_category_response(session: DbSession, category: object) -> CategoryResponse:
    response = CategoryResponse.model_validate(category)
    await session.commit()
    return response


@router.get("/tree", response_model=list[CategoryTreeItem])
async def category_tree(
    context: CurrentContext,
    session: DbSession,
    is_archived: bool | None = False,
) -> list[CategoryTreeItem]:
    items, _ = await repository.list_categories(
        session,
        context.workspace.id,
        category_type=None,
        parent_id=None,
        filter_parent=False,
        is_archived=is_archived,
        limit=1000,
        offset=0,
    )
    return service.build_tree(items)


@router.get("", response_model=CategoryPage)
async def category_list(
    context: CurrentContext,
    session: DbSession,
    category_type: CategoryType | None = None,
    parent_id: uuid.UUID | None = None,
    is_archived: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CategoryPage:
    items, total = await repository.list_categories(
        session,
        context.workspace.id,
        category_type=category_type,
        parent_id=parent_id,
        filter_parent=parent_id is not None,
        is_archived=is_archived,
        limit=limit,
        offset=offset,
    )
    return CategoryPage(
        items=[CategoryResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.post("", response_model=CategoryResponse, status_code=201)
async def category_create(
    data: CategoryCreate,
    context: EditorContext,
    session: DbSession,
) -> CategoryResponse:
    category = await service.create_category(session, context, data, commit=False)
    return await _commit_category_response(session, category)


@router.get("/{category_id}", response_model=CategoryResponse)
async def category_get(
    category_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
) -> CategoryResponse:
    category = await repository.get_category(session, context.workspace.id, category_id)
    if category is None:
        raise ApiError(status_code=404, code="CATEGORY_NOT_FOUND", message="Category was not found")
    return CategoryResponse.model_validate(category)


@router.patch("/{category_id}", response_model=CategoryResponse)
async def category_update(
    category_id: uuid.UUID,
    data: CategoryUpdate,
    context: EditorContext,
    session: DbSession,
) -> CategoryResponse:
    category = await service.update_category(session, context, category_id, data, commit=False)
    return await _commit_category_response(session, category)


@router.delete("/{category_id}", response_model=CategoryResponse)
async def category_delete(
    category_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    version: int = Query(ge=1),
) -> CategoryResponse:
    category = await service.delete_category(session, context, category_id, version, commit=False)
    return await _commit_category_response(session, category)


@router.post("/{category_id}/restore", response_model=CategoryResponse)
async def category_restore(
    category_id: uuid.UUID,
    data: VersionRequest,
    context: EditorContext,
    session: DbSession,
) -> CategoryResponse:
    category = await service.restore_category(
        session, context, category_id, data.version, commit=False
    )
    return await _commit_category_response(session, category)
