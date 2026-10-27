import secrets
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter
from sqlalchemy import select

from app.core.errors import ApiError
from app.db.models.automations import NotificationSetting, TelegramIntent, TelegramLink
from app.dependencies.context import CurrentContext
from app.dependencies.database import DbSession
from app.schemas.automations import (
    NotificationSettingInput,
    NotificationSettingResponse,
    TelegramIntegrationResponse,
)
from app.services import telegram as telegram_service

router = APIRouter()


@router.get("", response_model=list[NotificationSettingResponse])
async def notification_settings(
    context: CurrentContext,
    session: DbSession,
) -> list[NotificationSettingResponse]:
    items = list(
        (
            await session.scalars(
                select(NotificationSetting)
                .where(
                    NotificationSetting.workspace_id == context.workspace.id,
                    NotificationSetting.user_id == context.user.id,
                )
                .order_by(NotificationSetting.channel, NotificationSetting.event_type)
            )
        ).all()
    )
    return [NotificationSettingResponse.model_validate(item) for item in items]


@router.put("/telegram", response_model=NotificationSettingResponse)
async def update_telegram_notification(
    data: NotificationSettingInput,
    context: CurrentContext,
    session: DbSession,
) -> NotificationSettingResponse:
    try:
        ZoneInfo(data.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Notification timezone is invalid",
        ) from exc
    schedule = None
    if data.schedule_time is not None:
        try:
            schedule = time.fromisoformat(data.schedule_time)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="schedule_time must use HH:MM or HH:MM:SS",
            ) from exc
    setting = await session.scalar(
        select(NotificationSetting).where(
            NotificationSetting.workspace_id == context.workspace.id,
            NotificationSetting.user_id == context.user.id,
            NotificationSetting.channel == "telegram",
            NotificationSetting.event_type == data.event_type,
        )
    )
    if setting is None:
        setting = NotificationSetting(
            workspace_id=context.workspace.id,
            user_id=context.user.id,
            channel="telegram",
            event_type=data.event_type,
            enabled=data.enabled,
            schedule_time=schedule,
            timezone=data.timezone,
            configuration=data.configuration,
        )
        session.add(setting)
    else:
        setting.enabled = data.enabled
        setting.schedule_time = schedule
        setting.timezone = data.timezone
        setting.configuration = data.configuration
    await session.commit()
    await session.refresh(setting)
    return NotificationSettingResponse.model_validate(setting)


@router.post("/telegram/test", response_model=TelegramIntegrationResponse)
async def test_telegram_notification(
    context: CurrentContext,
    session: DbSession,
) -> TelegramIntegrationResponse:
    link = await telegram_service.status(session, context.user.id, context.workspace.id)
    if not link.linked:
        raise ApiError(
            status_code=409,
            code="TELEGRAM_NOT_LINKED",
            message="Telegram is not linked",
        )
    link_row = await session.scalar(
        select(TelegramLink).where(
            TelegramLink.user_id == context.user.id,
            TelegramLink.workspace_id == context.workspace.id,
            TelegramLink.status == "active",
        )
    )
    if link_row is None:
        raise ApiError(
            status_code=409,
            code="TELEGRAM_NOT_LINKED",
            message="Telegram is not linked",
        )
    intent = TelegramIntent(
        workspace_id=context.workspace.id,
        user_id=context.user.id,
        telegram_chat_id=link_row.telegram_chat_id,
        intent_type="notification_test",
        payload={"message": "Тестовое уведомление Финпространства."},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        created_at=datetime.now(UTC),
        opaque_id=secrets.token_urlsafe(12),
    )
    session.add(intent)
    await session.commit()
    return TelegramIntegrationResponse(
        status="queued",
        response_type="message",
        messages=["Тестовое уведомление поставлено в очередь n8n."],
    )
