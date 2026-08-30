import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, Query

from app.dependencies.context import CurrentContext, EditorContext
from app.dependencies.database import DbSession
from app.schemas.automations import (
    RecurringRuleCreate,
    RecurringRuleExecutionResponse,
    RecurringRuleHistoryPage,
    RecurringRulePage,
    RecurringRuleResponse,
    RecurringRuleUpdate,
)
from app.schemas.common import PageMeta
from app.services import recurring_rules as service

router = APIRouter()


@router.get("", response_model=RecurringRulePage)
async def recurring_rule_list(
    context: CurrentContext,
    session: DbSession,
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RecurringRulePage:
    items, total = await service.list_rules(
        session,
        context.workspace.id,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return RecurringRulePage(
        items=await service.rule_page_responses(session, context.workspace.id, items),
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.post("", response_model=RecurringRuleResponse, status_code=201)
async def recurring_rule_create(
    data: RecurringRuleCreate,
    context: EditorContext,
    session: DbSession,
) -> RecurringRuleResponse:
    return await service.rule_response(session, await service.create_rule(session, context, data))


@router.get("/{rule_id}", response_model=RecurringRuleResponse)
async def recurring_rule_get(
    rule_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
) -> RecurringRuleResponse:
    return await service.rule_response(
        session, await service.get_rule(session, context.workspace.id, rule_id)
    )


@router.patch("/{rule_id}", response_model=RecurringRuleResponse)
async def recurring_rule_update(
    rule_id: uuid.UUID,
    data: RecurringRuleUpdate,
    context: EditorContext,
    session: DbSession,
) -> RecurringRuleResponse:
    return await service.rule_response(
        session, await service.update_rule(session, context, rule_id, data)
    )


@router.delete("/{rule_id}", response_model=RecurringRuleResponse)
async def recurring_rule_delete(
    rule_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
) -> RecurringRuleResponse:
    return await service.rule_response(
        session, await service.change_state(session, context, rule_id, "delete")
    )


@router.post("/{rule_id}/restore", response_model=RecurringRuleResponse)
async def recurring_rule_restore(
    rule_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
) -> RecurringRuleResponse:
    return await service.rule_response(
        session, await service.change_state(session, context, rule_id, "restore")
    )


@router.post("/{rule_id}/pause", response_model=RecurringRuleResponse)
async def recurring_rule_pause(
    rule_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
) -> RecurringRuleResponse:
    return await service.rule_response(
        session, await service.change_state(session, context, rule_id, "pause")
    )


@router.post("/{rule_id}/resume", response_model=RecurringRuleResponse)
async def recurring_rule_resume(
    rule_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
) -> RecurringRuleResponse:
    return await service.rule_response(
        session, await service.change_state(session, context, rule_id, "resume")
    )


@router.post("/{rule_id}/run-now", response_model=RecurringRuleExecutionResponse)
async def recurring_rule_run_now(
    rule_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> RecurringRuleExecutionResponse:
    rule = await service.get_rule(session, context.workspace.id, rule_id)
    scheduled_for = datetime.now(UTC).replace(microsecond=0)
    execution, duplicate = await service.execute_rule(
        session,
        rule,
        scheduled_for=scheduled_for,
        idempotency_key=idempotency_key or f"manual-recurring:{rule.id}:{uuid.uuid4()}",
        service_account_id=None,
        initiated_by=context.user.id,
        request_id=context.request_id,
        trigger_type="manual",
    )
    return RecurringRuleExecutionResponse.model_validate(execution).model_copy(
        update={"duplicate": duplicate}
    )


@router.get("/{rule_id}/history", response_model=RecurringRuleHistoryPage)
async def recurring_rule_history(
    rule_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RecurringRuleHistoryPage:
    rule = await service.get_rule(session, context.workspace.id, rule_id)
    items, total = await service.list_history(session, rule, limit=limit, offset=offset)
    return RecurringRuleHistoryPage(
        items=[RecurringRuleExecutionResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )
