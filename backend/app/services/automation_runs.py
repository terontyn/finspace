import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.automations import AutomationRun
from app.services.audit import request_uuid


async def begin_run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID | None,
    automation_type: str,
    trigger_type: str,
    idempotency_key: str,
    service_account_id: uuid.UUID | None,
    initiated_by: uuid.UUID | None,
    request_id: str,
    input_summary: dict[str, Any] | None,
) -> tuple[AutomationRun, bool]:
    normalized = idempotency_key.strip()
    if not normalized or len(normalized) > 255:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="X-Idempotency-Key is required and must contain at most 255 characters",
        )
    existing = await session.scalar(
        select(AutomationRun).where(AutomationRun.idempotency_key == normalized)
    )
    if existing is not None:
        if (
            existing.workspace_id != workspace_id
            or existing.automation_type != automation_type
            or existing.service_account_id != service_account_id
        ):
            raise ApiError(
                status_code=409,
                code="AUTOMATION_IDEMPOTENCY_CONFLICT",
                message="Idempotency key was already used for another automation request",
            )
        return existing, True
    now = datetime.now(UTC)
    run = AutomationRun(
        workspace_id=workspace_id,
        automation_type=automation_type,
        trigger_type=trigger_type,
        idempotency_key=normalized,
        status="started",
        service_account_id=service_account_id,
        initiated_by=initiated_by,
        started_at=now,
        input_summary=input_summary,
        request_id=request_uuid(request_id),
        created_at=now,
    )
    session.add(run)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        existing = await session.scalar(
            select(AutomationRun).where(AutomationRun.idempotency_key == normalized)
        )
        if existing is None:
            raise
        if (
            existing.workspace_id != workspace_id
            or existing.automation_type != automation_type
            or existing.service_account_id != service_account_id
        ):
            raise ApiError(
                status_code=409,
                code="AUTOMATION_IDEMPOTENCY_CONFLICT",
                message="Idempotency key was already used for another automation request",
            ) from exc
        return existing, True
    return run, False


def complete_run(
    run: AutomationRun,
    result_summary: dict[str, Any],
    *,
    status: str = "completed",
) -> None:
    run.status = status
    run.result_summary = result_summary
    run.finished_at = datetime.now(UTC)
    run.error_code = None
    run.error_message = None


def fail_run(run: AutomationRun, code: str, message: str) -> None:
    run.status = "failed"
    run.error_code = code[:100]
    run.error_message = message[:1000]
    run.finished_at = datetime.now(UTC)


async def list_runs(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[AutomationRun], int]:
    filters = [AutomationRun.workspace_id == workspace_id]
    if status is not None:
        filters.append(AutomationRun.status == status)
    total = int(
        await session.scalar(select(func.count()).select_from(AutomationRun).where(*filters)) or 0
    )
    items = list(
        (
            await session.scalars(
                select(AutomationRun)
                .where(*filters)
                .order_by(AutomationRun.started_at.desc(), AutomationRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total
