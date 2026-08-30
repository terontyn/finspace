import uuid

from fastapi import APIRouter, Query

from app.dependencies.context import CurrentContext, EditorContext
from app.dependencies.database import DbSession
from app.repositories import categorization_rules as repository
from app.schemas.accounts import VersionRequest
from app.schemas.categorization_rules import (
    CategorizationPreviewRequest,
    CategorizationPreviewResponse,
    CategorizationRuleCreate,
    CategorizationRulePage,
    CategorizationRuleResponse,
    CategorizationRuleUpdate,
)
from app.schemas.common import PageMeta
from app.schemas.transactions import EntityRef
from app.services import categorization_rules as service

router = APIRouter()


@router.get("", response_model=CategorizationRulePage)
async def categorization_rule_list(
    context: CurrentContext,
    session: DbSession,
    include_deleted: bool = False,
    is_active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CategorizationRulePage:
    items, total = await repository.list_rules(
        session,
        context.workspace.id,
        include_deleted=include_deleted,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )
    return CategorizationRulePage(
        items=[CategorizationRuleResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.post("", response_model=CategorizationRuleResponse, status_code=201)
async def categorization_rule_create(
    data: CategorizationRuleCreate,
    context: EditorContext,
    session: DbSession,
) -> CategorizationRuleResponse:
    return CategorizationRuleResponse.model_validate(
        await service.create_rule(session, context, data)
    )


@router.post("/preview", response_model=CategorizationPreviewResponse)
async def categorization_rule_preview(
    data: CategorizationPreviewRequest,
    context: CurrentContext,
    session: DbSession,
) -> CategorizationPreviewResponse:
    _, match = await service.preview_transaction(
        session,
        context.workspace.id,
        data.transaction_id,
    )
    if match is None:
        return CategorizationPreviewResponse(matched=False, rule=None, category=None)
    return CategorizationPreviewResponse(
        matched=True,
        rule=CategorizationRuleResponse.model_validate(match.rule),
        category=EntityRef(id=match.category.id, name=match.category.name),
    )


@router.get("/{rule_id}", response_model=CategorizationRuleResponse)
async def categorization_rule_get(
    rule_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
) -> CategorizationRuleResponse:
    return CategorizationRuleResponse.model_validate(
        await service.get_rule(session, context.workspace.id, rule_id)
    )


@router.patch("/{rule_id}", response_model=CategorizationRuleResponse)
async def categorization_rule_update(
    rule_id: uuid.UUID,
    data: CategorizationRuleUpdate,
    context: EditorContext,
    session: DbSession,
) -> CategorizationRuleResponse:
    return CategorizationRuleResponse.model_validate(
        await service.update_rule(session, context, rule_id, data)
    )


@router.delete("/{rule_id}", response_model=CategorizationRuleResponse)
async def categorization_rule_delete(
    rule_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    version: int = Query(ge=1),
) -> CategorizationRuleResponse:
    return CategorizationRuleResponse.model_validate(
        await service.delete_rule(session, context, rule_id, version)
    )


@router.post("/{rule_id}/restore", response_model=CategorizationRuleResponse)
async def categorization_rule_restore(
    rule_id: uuid.UUID,
    data: VersionRequest,
    context: EditorContext,
    session: DbSession,
) -> CategorizationRuleResponse:
    return CategorizationRuleResponse.model_validate(
        await service.restore_rule(session, context, rule_id, data.version)
    )
