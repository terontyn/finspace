import uuid

from fastapi import APIRouter, Query

from app.dependencies.context import CurrentContext
from app.dependencies.database import DbSession
from app.schemas.categorization_apply import (
    CategorizationApplyOperationHistoryDetail,
    CategorizationApplyOperationHistoryPage,
)
from app.schemas.common import PageMeta
from app.services import categorization_apply_history as service

router = APIRouter()


@router.get("", response_model=CategorizationApplyOperationHistoryPage)
async def categorization_apply_operation_list(
    context: CurrentContext,
    session: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CategorizationApplyOperationHistoryPage:
    items, total = await service.list_operations(
        session,
        context.workspace.id,
        limit=limit,
        offset=offset,
    )
    return CategorizationApplyOperationHistoryPage(
        items=items,
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.get("/{operation_id}", response_model=CategorizationApplyOperationHistoryDetail)
async def categorization_apply_operation_detail(
    operation_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CategorizationApplyOperationHistoryDetail:
    return await service.get_operation_detail(
        session,
        context.workspace.id,
        operation_id,
        limit=limit,
        offset=offset,
    )
