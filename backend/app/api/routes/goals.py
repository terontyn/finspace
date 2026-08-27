import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query

from app.dependencies.context import CurrentContext, EditorContext
from app.dependencies.database import DbSession
from app.schemas.common import CurrencyCode, PageMeta
from app.schemas.goals import (
    GoalContributionCommandResponse,
    GoalContributionCreate,
    GoalContributionPage,
    GoalCorrectionCreate,
    GoalCreate,
    GoalPage,
    GoalResponse,
    GoalStatus,
    GoalUpdate,
    GoalVersionRequest,
)
from app.services import goals as service

router = APIRouter()
IdempotencyHeader = Annotated[str | None, Header(alias="X-Idempotency-Key")]


@router.get("", response_model=GoalPage)
async def goal_list(
    context: CurrentContext,
    session: DbSession,
    status: GoalStatus | None = None,
    currency: CurrencyCode | None = None,
    include_deleted: bool = False,
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> GoalPage:
    items, total = await service.list_goals(
        session,
        context.workspace,
        status=status,
        currency=currency,
        include_deleted=include_deleted,
        search=search,
        limit=limit,
        offset=offset,
    )
    return GoalPage(items=items, page=PageMeta(limit=limit, offset=offset, total=total))


@router.post("", response_model=GoalResponse, status_code=201)
async def goal_create(
    data: GoalCreate,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await service.create_goal(session, context, data, idempotency_key or "")


@router.get("/{goal_id}", response_model=GoalResponse)
async def goal_get(
    goal_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
    include_deleted: bool = False,
) -> GoalResponse:
    return await service.get_goal(
        session, context.workspace, goal_id, include_deleted=include_deleted
    )


@router.patch("/{goal_id}", response_model=GoalResponse)
async def goal_update(
    goal_id: uuid.UUID,
    data: GoalUpdate,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await service.update_goal(session, context, goal_id, data, idempotency_key or "")


async def _lifecycle(
    operation: str,
    goal_id: uuid.UUID,
    data: GoalVersionRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: str,
) -> GoalResponse:
    handlers = {
        "pause": service.pause_goal,
        "resume": service.resume_goal,
        "complete": service.complete_goal,
        "reopen": service.reopen_goal,
        "cancel": service.cancel_goal,
    }
    return await handlers[operation](session, context, goal_id, data.version, idempotency_key)


@router.post("/{goal_id}/pause", response_model=GoalResponse)
async def goal_pause(
    goal_id: uuid.UUID,
    data: GoalVersionRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await _lifecycle("pause", goal_id, data, context, session, idempotency_key or "")


@router.post("/{goal_id}/resume", response_model=GoalResponse)
async def goal_resume(
    goal_id: uuid.UUID,
    data: GoalVersionRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await _lifecycle("resume", goal_id, data, context, session, idempotency_key or "")


@router.post("/{goal_id}/complete", response_model=GoalResponse)
async def goal_complete(
    goal_id: uuid.UUID,
    data: GoalVersionRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await _lifecycle("complete", goal_id, data, context, session, idempotency_key or "")


@router.post("/{goal_id}/reopen", response_model=GoalResponse)
async def goal_reopen(
    goal_id: uuid.UUID,
    data: GoalVersionRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await _lifecycle("reopen", goal_id, data, context, session, idempotency_key or "")


@router.post("/{goal_id}/cancel", response_model=GoalResponse)
async def goal_cancel(
    goal_id: uuid.UUID,
    data: GoalVersionRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await _lifecycle("cancel", goal_id, data, context, session, idempotency_key or "")


@router.delete("/{goal_id}", response_model=GoalResponse)
async def goal_delete(
    goal_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    version: int = Query(ge=1),
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await service.delete_goal(session, context, goal_id, version, idempotency_key or "")


@router.post("/{goal_id}/restore", response_model=GoalResponse)
async def goal_restore(
    goal_id: uuid.UUID,
    data: GoalVersionRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalResponse:
    return await service.restore_goal(
        session, context, goal_id, data.version, idempotency_key or ""
    )


@router.get("/{goal_id}/contributions", response_model=GoalContributionPage)
async def goal_contribution_list(
    goal_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
    include_deleted: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> GoalContributionPage:
    items, total = await service.list_contributions(
        session,
        context.workspace.id,
        goal_id,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return GoalContributionPage(
        items=items,
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.post(
    "/{goal_id}/contributions",
    response_model=GoalContributionCommandResponse,
    status_code=201,
)
async def goal_contribution_create(
    goal_id: uuid.UUID,
    data: GoalContributionCreate,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalContributionCommandResponse:
    return await service.add_contribution(session, context, goal_id, data, idempotency_key or "")


@router.post(
    "/{goal_id}/contributions/{contribution_id}/correct",
    response_model=GoalContributionCommandResponse,
    status_code=201,
)
async def goal_contribution_correct(
    goal_id: uuid.UUID,
    contribution_id: uuid.UUID,
    data: GoalCorrectionCreate,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> GoalContributionCommandResponse:
    return await service.correct_contribution(
        session,
        context,
        goal_id,
        contribution_id,
        data,
        idempotency_key or "",
    )
