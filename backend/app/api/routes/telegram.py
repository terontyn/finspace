from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select

from app.core.errors import ApiError
from app.db.models.automations import TelegramIntent
from app.dependencies.context import CurrentContext
from app.dependencies.database import DbSession
from app.dependencies.service_account import ServiceAccountContext, require_service_permission
from app.schemas.automations import (
    TelegramCallbackRequest,
    TelegramDeliveryStatusRequest,
    TelegramIntegrationResponse,
    TelegramLinkCodeResponse,
    TelegramLinkRequest,
    TelegramLinkStatusResponse,
    TelegramMessageRequest,
)
from app.services import automation_runs
from app.services import telegram as service
from app.services.audit import record_audit

router = APIRouter()
settings_router = APIRouter()
TelegramServiceContext = Annotated[
    ServiceAccountContext, Depends(require_service_permission("notifications:send"))
]


@settings_router.post("/link-code", response_model=TelegramLinkCodeResponse, status_code=201)
async def telegram_link_code(
    context: CurrentContext,
    session: DbSession,
) -> TelegramLinkCodeResponse:
    code, row = await service.create_link_code(session, context)
    return TelegramLinkCodeResponse(
        code=code,
        expires_at=row.expires_at,
        warning="Код одноразовый и действует не более 10 минут.",
    )


@settings_router.get("", response_model=TelegramLinkStatusResponse)
async def telegram_status(
    context: CurrentContext,
    session: DbSession,
) -> TelegramLinkStatusResponse:
    return await service.status(session, context.user.id, context.workspace.id)


@settings_router.delete("", response_model=TelegramLinkStatusResponse)
async def telegram_revoke(
    context: CurrentContext,
    session: DbSession,
) -> TelegramLinkStatusResponse:
    return await service.revoke_link(session, context)


@router.post("/link", response_model=TelegramIntegrationResponse)
async def telegram_link(
    data: TelegramLinkRequest,
    context: TelegramServiceContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> TelegramIntegrationResponse:
    return await _idempotent(
        session,
        context,
        idempotency_key or "",
        "telegram_link",
        {"telegram_user_id": str(data.telegram_user_id)},
        lambda: service.link(
            session,
            data,
            service_workspace_id=context.workspace_id,
            request_id=context.request_id,
        ),
    )


@router.post("/message", response_model=TelegramIntegrationResponse)
async def telegram_message(
    data: TelegramMessageRequest,
    context: TelegramServiceContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> TelegramIntegrationResponse:
    return await _idempotent(
        session,
        context,
        idempotency_key or "",
        "telegram_message",
        {"telegram_user_id": str(data.telegram_user_id), "update_id": data.update_id},
        lambda: service.handle_message(
            session,
            data,
            service_workspace_id=context.workspace_id,
            request_id=context.request_id,
        ),
    )


@router.post("/callback", response_model=TelegramIntegrationResponse)
async def telegram_callback(
    data: TelegramCallbackRequest,
    context: TelegramServiceContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> TelegramIntegrationResponse:
    return await _idempotent(
        session,
        context,
        idempotency_key or "",
        "telegram_callback",
        {"telegram_user_id": str(data.telegram_user_id), "update_id": data.update_id},
        lambda: service.handle_callback(
            session,
            data,
            service_workspace_id=context.workspace_id,
            request_id=context.request_id,
        ),
    )


@router.post("/delivery-status", response_model=TelegramIntegrationResponse)
async def telegram_delivery_status(
    data: TelegramDeliveryStatusRequest,
    context: TelegramServiceContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> TelegramIntegrationResponse:
    async def response() -> TelegramIntegrationResponse:
        link_row = await service.validate_delivery_target(
            session,
            data.telegram_user_id,
            data.telegram_chat_id,
            service_workspace_id=context.workspace_id,
        )
        if data.status == "failed" and not data.error_code:
            raise ApiError(
                status_code=422,
                code="TELEGRAM_DELIVERY_FAILED",
                message="Failed delivery requires an error code",
            )
        notification = await session.scalar(
            select(TelegramIntent).where(
                TelegramIntent.opaque_id == data.delivery_id,
                TelegramIntent.intent_type == "notification_test",
                TelegramIntent.workspace_id == link_row.workspace_id,
                TelegramIntent.user_id == link_row.user_id,
                TelegramIntent.telegram_chat_id == link_row.telegram_chat_id,
            )
        )
        if notification is not None:
            notification.status = "failed" if data.status == "failed" else "confirmed"
            notification.resolved_at = datetime.now(UTC)
        return TelegramIntegrationResponse(
            status=data.status,
            response_type="delivery",
            messages=[],
        )

    return await _idempotent(
        session,
        context,
        idempotency_key or "",
        "telegram_delivery",
        {"delivery_id": data.delivery_id, "status": data.status},
        response,
    )


async def _idempotent(
    session: DbSession,
    context: ServiceAccountContext,
    idempotency_key: str,
    automation_type: str,
    input_summary: dict[str, object],
    action: Callable[[], Awaitable[TelegramIntegrationResponse]],
) -> TelegramIntegrationResponse:
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=context.workspace_id,
        automation_type=automation_type,
        trigger_type="telegram",
        idempotency_key=idempotency_key,
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        input_summary=input_summary,
    )
    if duplicate:
        if run.result_summary is None:
            raise ApiError(
                status_code=409,
                code="AUTOMATION_IDEMPOTENCY_CONFLICT",
                message="The previous Telegram request is still running",
            )
        return TelegramIntegrationResponse.model_validate(run.result_summary).model_copy(
            update={"duplicate": True}
        )
    try:
        result = await action()
    except ApiError as exc:
        automation_runs.fail_run(run, exc.code, exc.message)
        await session.commit()
        raise
    automation_runs.complete_run(run, result.model_dump(mode="json"))
    if context.workspace_id is not None:
        await record_audit(
            session,
            workspace_id=context.workspace_id,
            actor_user_id=None,
            entity_type="automation_run",
            entity_id=run.id,
            action="automation.run",
            before_data=None,
            after_data={"automation_type": automation_type, "status": "completed"},
            request_id=context.request_id,
            source="automation",
        )
    await session.commit()
    return result
