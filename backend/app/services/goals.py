import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.goals import Goal, GoalCommandResult, GoalContribution
from app.db.models.users import Workspace
from app.dependencies.context import RequestContext
from app.repositories import goals as repository
from app.schemas.goals import (
    GoalContributionCommandResponse,
    GoalContributionCreate,
    GoalContributionResponse,
    GoalCorrectionCreate,
    GoalCreate,
    GoalResponse,
    GoalUpdate,
)
from app.services.audit import record_audit, request_uuid, snapshot
from app.services.month_close_fingerprint import (
    canonical_datetime,
    canonical_decimal,
    hash_canonical,
)

MONEY_STEP = Decimal("0.0001")
ZERO = Decimal("0.0000")
FUTURE_SKEW = timedelta(minutes=5)
GoalOperation = Literal[
    "create",
    "update",
    "pause",
    "resume",
    "complete",
    "reopen",
    "cancel",
    "delete",
    "restore",
    "contribution",
    "correction",
]


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_STEP)


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="X-Idempotency-Key is required and must contain at most 255 characters",
        )
    return normalized


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        return canonical_datetime(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return cast(Any, value).isoformat()
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    return value


def _request_hash(
    operation: GoalOperation,
    workspace_id: uuid.UUID,
    *,
    goal_id: uuid.UUID | None,
    contribution_id: uuid.UUID | None,
    payload: dict[str, object],
) -> str:
    return hash_canonical(
        {
            "operation": operation,
            "workspace_id": str(workspace_id),
            "goal_id": str(goal_id) if goal_id is not None else None,
            "contribution_id": (str(contribution_id) if contribution_id is not None else None),
            "payload": _canonical_value(payload),
        }
    )


async def _lock_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    locked = await session.scalar(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    if locked is None:
        raise ApiError(status_code=404, code="GOAL_NOT_FOUND", message="Workspace was not found")


async def _replay[ResponseModel: BaseModel](
    session: AsyncSession,
    workspace_id: uuid.UUID,
    key: str,
    request_hash: str,
    response_type: type[ResponseModel],
) -> ResponseModel | None:
    result = await repository.command_result_for_key(session, workspace_id, key)
    if result is None:
        return None
    if result.request_hash != request_hash:
        raise ApiError(
            status_code=409,
            code="GOAL_IDEMPOTENCY_CONFLICT",
            message="Idempotency key was already used for another Goal command",
        )
    return response_type.model_validate(result.response_snapshot)


async def _begin_command[ResponseModel: BaseModel](
    session: AsyncSession,
    context: RequestContext,
    idempotency_key: str,
    request_hash: str,
    response_type: type[ResponseModel],
) -> tuple[str, ResponseModel | None]:
    key = _idempotency_key(idempotency_key)
    await _lock_workspace(session, context.workspace.id)
    return key, await _replay(session, context.workspace.id, key, request_hash, response_type)


async def _record_result(
    session: AsyncSession,
    context: RequestContext,
    *,
    goal_id: uuid.UUID,
    contribution_id: uuid.UUID | None,
    operation: GoalOperation,
    key: str,
    request_hash: str,
    response_status: int,
    response: BaseModel,
) -> None:
    session.add(
        GoalCommandResult(
            workspace_id=context.workspace.id,
            goal_id=goal_id,
            contribution_id=contribution_id,
            command_type=operation,
            idempotency_key=key,
            request_hash=request_hash,
            response_status=response_status,
            response_snapshot=response.model_dump(mode="json"),
            actor_user_id=context.user.id,
            request_id=request_uuid(context.request_id),
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()


def _goal_response(
    goal: Goal,
    contributed_amount: Decimal,
    contribution_count: int,
    timezone: str,
) -> GoalResponse:
    contributed = money(contributed_amount)
    target = money(goal.target_amount)
    remaining = money(target - contributed)
    percent = money(contributed / target * Decimal("100"))
    local_today = datetime.now(UTC).astimezone(ZoneInfo(timezone)).date()
    days_remaining = (goal.target_date - local_today).days if goal.target_date is not None else None
    overdue = bool(
        goal.deleted_at is None
        and goal.status in {"active", "paused"}
        and days_remaining is not None
        and days_remaining < 0
    )
    return GoalResponse(
        id=goal.id,
        workspace_id=goal.workspace_id,
        name=goal.name,
        description=goal.description,
        currency=goal.currency,
        target_amount=target,
        target_date=goal.target_date,
        status=cast(Any, goal.status),
        version=goal.version,
        deleted_at=goal.deleted_at,
        created_by=goal.created_by,
        updated_by=goal.updated_by,
        created_at=goal.created_at,
        updated_at=goal.updated_at,
        contributed_amount=contributed,
        remaining_amount=remaining,
        progress_percent=percent,
        is_target_reached=contributed >= target,
        contribution_count=contribution_count,
        days_remaining=days_remaining,
        overdue=overdue,
    )


async def _current_response(
    session: AsyncSession, context: RequestContext, goal: Goal
) -> GoalResponse:
    amount, count = await repository.aggregate_for_goal(session, context.workspace.id, goal.id)
    return _goal_response(goal, amount, count, context.workspace.timezone)


def _contribution_response(
    contribution: GoalContribution, display_name: str | None
) -> GoalContributionResponse:
    return GoalContributionResponse(
        id=contribution.id,
        goal_id=contribution.goal_id,
        workspace_id=contribution.workspace_id,
        currency=contribution.currency,
        amount=money(contribution.amount),
        note=contribution.note,
        contributed_at=contribution.contributed_at,
        correction_of_id=contribution.correction_of_id,
        created_by=contribution.created_by,
        created_by_display_name=display_name,
        created_at=contribution.created_at,
    )


def _goal_after(goal: Goal, operation: str) -> dict[str, Any]:
    return {**snapshot("goal", goal), "goal_operation": operation}


def _not_found() -> ApiError:
    return ApiError(status_code=404, code="GOAL_NOT_FOUND", message="Goal was not found")


def _require_live(goal: Goal) -> None:
    if goal.deleted_at is not None:
        raise ApiError(
            status_code=409,
            code="GOAL_RESTORE_REQUIRED",
            message="Deleted Goal must be restored before this command",
        )


def _require_version(goal: Goal, version: int) -> None:
    if goal.version != version:
        raise ApiError(
            status_code=409,
            code="GOAL_VERSION_CONFLICT",
            message="Goal was modified by another command",
            details={"current_version": goal.version},
        )


def _contribution_time(value: datetime | None, now: datetime) -> datetime:
    contributed_at = (value or now).astimezone(UTC)
    if contributed_at > now + FUTURE_SKEW:
        raise ApiError(
            status_code=422,
            code="GOAL_CONTRIBUTION_INVALID",
            message="Goal contribution cannot be more than five minutes in the future",
        )
    return contributed_at


async def list_goals(
    session: AsyncSession,
    workspace: Workspace,
    *,
    status: str | None,
    currency: str | None,
    include_deleted: bool,
    search: str | None,
    limit: int,
    offset: int,
) -> tuple[list[GoalResponse], int]:
    rows, total = await repository.list_goals(
        session,
        workspace.id,
        status=status,
        currency=currency,
        include_deleted=include_deleted,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [
        _goal_response(goal, amount, count, workspace.timezone) for goal, amount, count in rows
    ], total


async def get_goal(
    session: AsyncSession,
    workspace: Workspace,
    goal_id: uuid.UUID,
    *,
    include_deleted: bool,
) -> GoalResponse:
    row = await repository.get_goal_projection(
        session, workspace.id, goal_id, include_deleted=include_deleted
    )
    if row is None:
        raise _not_found()
    goal, amount, count = row
    return _goal_response(goal, amount, count, workspace.timezone)


async def create_goal(
    session: AsyncSession,
    context: RequestContext,
    data: GoalCreate,
    idempotency_key: str,
) -> GoalResponse:
    request_hash = _request_hash(
        "create",
        context.workspace.id,
        goal_id=None,
        contribution_id=None,
        payload=data.model_dump(),
    )
    try:
        key, replay = await _begin_command(
            session, context, idempotency_key, request_hash, GoalResponse
        )
        if replay is not None:
            return replay
        now = datetime.now(UTC)
        goal = Goal(
            workspace_id=context.workspace.id,
            name=data.name,
            description=data.description,
            currency=data.currency,
            target_amount=money(data.target_amount),
            target_date=data.target_date,
            status="active",
            version=1,
            created_by=context.user.id,
            updated_by=context.user.id,
            created_at=now,
            updated_at=now,
        )
        session.add(goal)
        await session.flush()
        response = _goal_response(goal, ZERO, 0, context.workspace.timezone)
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="goal",
            entity_id=goal.id,
            action="create",
            before_data=None,
            after_data=_goal_after(goal, "create"),
            request_id=context.request_id,
        )
        await _record_result(
            session,
            context,
            goal_id=goal.id,
            contribution_id=None,
            operation="create",
            key=key,
            request_hash=request_hash,
            response_status=201,
            response=response,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


def _update_payload(data: GoalUpdate) -> dict[str, object]:
    return data.model_dump(exclude_unset=True)


async def update_goal(
    session: AsyncSession,
    context: RequestContext,
    goal_id: uuid.UUID,
    data: GoalUpdate,
    idempotency_key: str,
) -> GoalResponse:
    payload = _update_payload(data)
    request_hash = _request_hash(
        "update",
        context.workspace.id,
        goal_id=goal_id,
        contribution_id=None,
        payload=payload,
    )
    try:
        key, replay = await _begin_command(
            session, context, idempotency_key, request_hash, GoalResponse
        )
        if replay is not None:
            return replay
        goal = await repository.get_goal(
            session, context.workspace.id, goal_id, include_deleted=True, for_update=True
        )
        if goal is None:
            raise _not_found()
        _require_live(goal)
        _require_version(goal, data.version)
        if goal.status not in {"active", "paused"}:
            raise ApiError(
                status_code=409,
                code="GOAL_STATUS_INVALID",
                message="Completed or cancelled Goal metadata cannot be changed",
                details={"status": goal.status},
            )
        changes = data.model_dump(exclude_unset=True, exclude={"version"})
        if "currency" in changes and changes["currency"] != goal.currency:
            _, count = await repository.aggregate_for_goal(session, context.workspace.id, goal.id)
            if count > 0:
                raise ApiError(
                    status_code=409,
                    code="GOAL_CURRENCY_IMMUTABLE",
                    message="Goal currency cannot change after the first contribution event",
                )
        before = snapshot("goal", goal)
        for field, value in changes.items():
            setattr(goal, field, value)
        now = datetime.now(UTC)
        goal.target_amount = money(goal.target_amount)
        goal.updated_by = context.user.id
        goal.updated_at = now
        goal.version += 1
        await session.flush()
        response = await _current_response(session, context, goal)
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="goal",
            entity_id=goal.id,
            action="update",
            before_data=before,
            after_data=_goal_after(goal, "update"),
            request_id=context.request_id,
        )
        await _record_result(
            session,
            context,
            goal_id=goal.id,
            contribution_id=None,
            operation="update",
            key=key,
            request_hash=request_hash,
            response_status=200,
            response=response,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def _lifecycle(
    session: AsyncSession,
    context: RequestContext,
    goal_id: uuid.UUID,
    version: int,
    operation: Literal["pause", "resume", "complete", "reopen", "cancel"],
    idempotency_key: str,
) -> GoalResponse:
    request_hash = _request_hash(
        operation,
        context.workspace.id,
        goal_id=goal_id,
        contribution_id=None,
        payload={"version": version},
    )
    transitions = {
        "pause": ({"active"}, "paused"),
        "resume": ({"paused"}, "active"),
        "complete": ({"active", "paused"}, "completed"),
        "reopen": ({"completed"}, "active"),
        "cancel": ({"active", "paused"}, "cancelled"),
    }
    try:
        key, replay = await _begin_command(
            session, context, idempotency_key, request_hash, GoalResponse
        )
        if replay is not None:
            return replay
        goal = await repository.get_goal(
            session, context.workspace.id, goal_id, include_deleted=True, for_update=True
        )
        if goal is None:
            raise _not_found()
        _require_live(goal)
        _require_version(goal, version)
        allowed, next_status = transitions[operation]
        if goal.status not in allowed:
            raise ApiError(
                status_code=409,
                code="GOAL_STATUS_INVALID",
                message="Goal lifecycle transition is not allowed",
                details={"status": goal.status, "operation": operation},
            )
        amount, count = await repository.aggregate_for_goal(session, context.workspace.id, goal.id)
        if operation == "complete" and money(amount) < money(goal.target_amount):
            raise ApiError(
                status_code=409,
                code="GOAL_TARGET_NOT_REACHED",
                message="Goal can be completed only after its target is reached",
            )
        before = snapshot("goal", goal)
        goal.status = next_status
        goal.updated_by = context.user.id
        goal.updated_at = datetime.now(UTC)
        goal.version += 1
        await session.flush()
        response = _goal_response(goal, amount, count, context.workspace.timezone)
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="goal",
            entity_id=goal.id,
            action="update",
            before_data=before,
            after_data=_goal_after(goal, operation),
            request_id=context.request_id,
        )
        await _record_result(
            session,
            context,
            goal_id=goal.id,
            contribution_id=None,
            operation=operation,
            key=key,
            request_hash=request_hash,
            response_status=200,
            response=response,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def pause_goal(
    session: AsyncSession, context: RequestContext, goal_id: uuid.UUID, version: int, key: str
) -> GoalResponse:
    return await _lifecycle(session, context, goal_id, version, "pause", key)


async def resume_goal(
    session: AsyncSession, context: RequestContext, goal_id: uuid.UUID, version: int, key: str
) -> GoalResponse:
    return await _lifecycle(session, context, goal_id, version, "resume", key)


async def complete_goal(
    session: AsyncSession, context: RequestContext, goal_id: uuid.UUID, version: int, key: str
) -> GoalResponse:
    return await _lifecycle(session, context, goal_id, version, "complete", key)


async def reopen_goal(
    session: AsyncSession, context: RequestContext, goal_id: uuid.UUID, version: int, key: str
) -> GoalResponse:
    return await _lifecycle(session, context, goal_id, version, "reopen", key)


async def cancel_goal(
    session: AsyncSession, context: RequestContext, goal_id: uuid.UUID, version: int, key: str
) -> GoalResponse:
    return await _lifecycle(session, context, goal_id, version, "cancel", key)


async def _delete_or_restore(
    session: AsyncSession,
    context: RequestContext,
    goal_id: uuid.UUID,
    version: int,
    operation: Literal["delete", "restore"],
    idempotency_key: str,
) -> GoalResponse:
    request_hash = _request_hash(
        operation,
        context.workspace.id,
        goal_id=goal_id,
        contribution_id=None,
        payload={"version": version},
    )
    try:
        key, replay = await _begin_command(
            session, context, idempotency_key, request_hash, GoalResponse
        )
        if replay is not None:
            return replay
        goal = await repository.get_goal(
            session, context.workspace.id, goal_id, include_deleted=True, for_update=True
        )
        if goal is None:
            raise _not_found()
        _require_version(goal, version)
        if operation == "delete" and goal.deleted_at is not None:
            raise ApiError(
                status_code=409,
                code="GOAL_RESTORE_REQUIRED",
                message="Goal is already deleted",
            )
        if operation == "restore" and goal.deleted_at is None:
            raise ApiError(
                status_code=409,
                code="GOAL_STATUS_INVALID",
                message="Only a deleted Goal can be restored",
            )
        before = snapshot("goal", goal)
        now = datetime.now(UTC)
        goal.deleted_at = now if operation == "delete" else None
        goal.updated_by = context.user.id
        goal.updated_at = now
        goal.version += 1
        await session.flush()
        response = await _current_response(session, context, goal)
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="goal",
            entity_id=goal.id,
            action=operation,
            before_data=before,
            after_data=_goal_after(goal, operation),
            request_id=context.request_id,
        )
        await _record_result(
            session,
            context,
            goal_id=goal.id,
            contribution_id=None,
            operation=operation,
            key=key,
            request_hash=request_hash,
            response_status=200,
            response=response,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def delete_goal(
    session: AsyncSession, context: RequestContext, goal_id: uuid.UUID, version: int, key: str
) -> GoalResponse:
    return await _delete_or_restore(session, context, goal_id, version, "delete", key)


async def restore_goal(
    session: AsyncSession, context: RequestContext, goal_id: uuid.UUID, version: int, key: str
) -> GoalResponse:
    return await _delete_or_restore(session, context, goal_id, version, "restore", key)


async def list_contributions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    goal_id: uuid.UUID,
    *,
    include_deleted: bool,
    limit: int,
    offset: int,
) -> tuple[list[GoalContributionResponse], int]:
    goal = await repository.get_goal(
        session, workspace_id, goal_id, include_deleted=include_deleted
    )
    if goal is None:
        raise _not_found()
    rows, total = await repository.list_contributions(
        session, workspace_id, goal_id, limit=limit, offset=offset
    )
    return [_contribution_response(item, display_name) for item, display_name in rows], total


async def add_contribution(
    session: AsyncSession,
    context: RequestContext,
    goal_id: uuid.UUID,
    data: GoalContributionCreate,
    idempotency_key: str,
) -> GoalContributionCommandResponse:
    payload: dict[str, object] = {
        "amount": data.amount,
        "note": data.note,
        "contributed_at": data.contributed_at,
    }
    request_hash = _request_hash(
        "contribution",
        context.workspace.id,
        goal_id=goal_id,
        contribution_id=None,
        payload=payload,
    )
    try:
        key, replay = await _begin_command(
            session,
            context,
            idempotency_key,
            request_hash,
            GoalContributionCommandResponse,
        )
        if replay is not None:
            return replay
        goal = await repository.get_goal(
            session, context.workspace.id, goal_id, include_deleted=True, for_update=True
        )
        if goal is None:
            raise _not_found()
        _require_live(goal)
        if goal.status != "active":
            raise ApiError(
                status_code=409,
                code="GOAL_CONTRIBUTION_NOT_ALLOWED",
                message="Normal contributions are allowed only for active Goals",
                details={"status": goal.status},
            )
        now = datetime.now(UTC)
        contribution = GoalContribution(
            workspace_id=context.workspace.id,
            goal_id=goal.id,
            currency=goal.currency,
            amount=money(data.amount),
            note=data.note,
            contributed_at=_contribution_time(data.contributed_at, now),
            correction_of_id=None,
            created_by=context.user.id,
            request_id=request_uuid(context.request_id),
            created_at=now,
        )
        session.add(contribution)
        await session.flush()
        goal_response = await _current_response(session, context, goal)
        response = GoalContributionCommandResponse(
            goal=goal_response,
            contribution=_contribution_response(contribution, context.user.display_name),
        )
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="goal_contribution",
            entity_id=contribution.id,
            action="create",
            before_data=None,
            after_data={
                "goal_operation": "contribution",
                "goal_id": str(goal.id),
                "contribution_id": str(contribution.id),
                "currency": contribution.currency,
                "amount": canonical_decimal(contribution.amount),
                "correction_of_id": None,
            },
            request_id=context.request_id,
        )
        await _record_result(
            session,
            context,
            goal_id=goal.id,
            contribution_id=contribution.id,
            operation="contribution",
            key=key,
            request_hash=request_hash,
            response_status=201,
            response=response,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def correct_contribution(
    session: AsyncSession,
    context: RequestContext,
    goal_id: uuid.UUID,
    contribution_id: uuid.UUID,
    data: GoalCorrectionCreate,
    idempotency_key: str,
) -> GoalContributionCommandResponse:
    payload: dict[str, object] = {
        "adjustment_amount": data.adjustment_amount,
        "note": data.note,
        "contributed_at": data.contributed_at,
    }
    request_hash = _request_hash(
        "correction",
        context.workspace.id,
        goal_id=goal_id,
        contribution_id=contribution_id,
        payload=payload,
    )
    try:
        key, replay = await _begin_command(
            session,
            context,
            idempotency_key,
            request_hash,
            GoalContributionCommandResponse,
        )
        if replay is not None:
            return replay
        goal = await repository.get_goal(
            session, context.workspace.id, goal_id, include_deleted=True, for_update=True
        )
        if goal is None:
            raise _not_found()
        _require_live(goal)
        original = await repository.contribution_for_goal(
            session,
            context.workspace.id,
            goal.id,
            contribution_id,
            for_update=True,
        )
        if original is None or original.correction_of_id is not None:
            raise ApiError(
                status_code=409,
                code="GOAL_CORRECTION_INVALID",
                message="Correction must reference an original contribution from this Goal",
            )
        adjustment = money(data.adjustment_amount)
        existing_corrections = await repository.correction_total(
            session, context.workspace.id, goal.id, original.id
        )
        effective_original = money(original.amount + existing_corrections + adjustment)
        total, _ = await repository.aggregate_for_goal(session, context.workspace.id, goal.id)
        proposed_total = money(total + adjustment)
        if effective_original < ZERO or proposed_total < ZERO:
            raise ApiError(
                status_code=422,
                code="GOAL_CORRECTION_INVALID",
                message="Correction cannot make the original contribution or Goal total negative",
                details={"effective_original_amount": canonical_decimal(effective_original)},
            )
        now = datetime.now(UTC)
        correction = GoalContribution(
            workspace_id=context.workspace.id,
            goal_id=goal.id,
            currency=goal.currency,
            amount=adjustment,
            note=data.note,
            contributed_at=_contribution_time(data.contributed_at, now),
            correction_of_id=original.id,
            created_by=context.user.id,
            request_id=request_uuid(context.request_id),
            created_at=now,
        )
        session.add(correction)
        await session.flush()
        response = GoalContributionCommandResponse(
            goal=await _current_response(session, context, goal),
            contribution=_contribution_response(correction, context.user.display_name),
        )
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="goal_contribution",
            entity_id=correction.id,
            action="create",
            before_data=None,
            after_data={
                "goal_operation": "correction",
                "goal_id": str(goal.id),
                "contribution_id": str(correction.id),
                "currency": correction.currency,
                "amount": canonical_decimal(correction.amount),
                "correction_of_id": str(original.id),
            },
            request_id=context.request_id,
        )
        await _record_result(
            session,
            context,
            goal_id=goal.id,
            contribution_id=correction.id,
            operation="correction",
            key=key,
            request_hash=request_hash,
            response_status=201,
            response=response,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise
