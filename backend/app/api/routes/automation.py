import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.automations import (
    AutomationRun,
    NotificationSetting,
    RecurringRule,
    ServiceAccount,
    TelegramIntent,
    TelegramLink,
)
from app.db.models.users import Workspace
from app.dependencies.context import CurrentContext
from app.dependencies.database import DbSession
from app.dependencies.service_account import (
    ServiceAccountContext,
    ensure_service_workspace,
    require_service_permission,
)
from app.schemas.automations import (
    AutomationHeartbeatRequest,
    AutomationHeartbeatResponse,
    AutomationRunPage,
    AutomationRunResponse,
    AutomationStatusResponse,
    BackupStatusResponse,
    MonthClosureResponse,
    NotificationEventType,
    NotificationTarget,
    NotificationTargetResponse,
    PendingNotification,
    PendingNotificationResponse,
    RecurringDueItem,
    RecurringDueResponse,
    RecurringExecuteRequest,
    RecurringRuleExecutionResponse,
    ReportRequest,
    UncategorizedReportResponse,
    WeeklyReportRequest,
    WeeklyReportResponse,
)
from app.schemas.common import PageMeta
from app.services import automation_runs, reports
from app.services import month_close as month_close_service
from app.services import recurring_rules as recurring_service
from app.services import service_accounts as service_account_service
from app.services.audit import record_audit
from app.services.backup_status import get_backup_status

router = APIRouter()
AutomationExecuteContext = Annotated[
    ServiceAccountContext, Depends(require_service_permission("automation:execute"))
]
RecurringReadContext = Annotated[
    ServiceAccountContext, Depends(require_service_permission("recurring:read"))
]
RecurringExecuteContext = Annotated[
    ServiceAccountContext, Depends(require_service_permission("recurring:execute"))
]
ReportsContext = Annotated[
    ServiceAccountContext, Depends(require_service_permission("reports:generate"))
]
BackupStatusContext = Annotated[
    ServiceAccountContext, Depends(require_service_permission("backup:status"))
]
NotificationContext = Annotated[
    ServiceAccountContext, Depends(require_service_permission("notifications:send"))
]
MonthClosePrepareContext = Annotated[
    ServiceAccountContext, Depends(require_service_permission("month-close:prepare"))
]


@router.post("/heartbeat", response_model=AutomationHeartbeatResponse)
async def automation_heartbeat(
    data: AutomationHeartbeatRequest,
    context: AutomationExecuteContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> AutomationHeartbeatResponse:
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=context.workspace_id,
        automation_type="n8n_heartbeat",
        trigger_type="heartbeat",
        idempotency_key=idempotency_key or "",
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        input_summary={
            "n8n_version": data.n8n_version,
            "workflow_count": data.workflow_count,
        },
    )
    if not duplicate:
        automation_runs.complete_run(
            run,
            {"accepted": True, "workflow_count": data.workflow_count},
        )
        if context.workspace_id is not None:
            await record_audit(
                session,
                workspace_id=context.workspace_id,
                actor_user_id=None,
                entity_type="automation_run",
                entity_id=run.id,
                action="automation.run",
                before_data=None,
                after_data={"automation_type": "n8n_heartbeat", "status": "completed"},
                request_id=context.request_id,
                source="automation",
            )
        await session.commit()
        await session.refresh(run)
    return AutomationHeartbeatResponse(
        status="accepted",
        run=AutomationRunResponse.model_validate(run),
        duplicate=duplicate,
    )


@router.get("/status", response_model=AutomationStatusResponse)
async def automation_status(
    context: CurrentContext,
    session: DbSession,
) -> AutomationStatusResponse:
    account = await session.scalar(
        select(ServiceAccount)
        .where(
            ServiceAccount.workspace_id == context.workspace.id,
            ServiceAccount.service_type == "n8n",
            ServiceAccount.status == "active",
        )
        .order_by(ServiceAccount.created_at.desc())
        .limit(1)
    )
    successful = await session.scalar(
        select(AutomationRun)
        .where(
            AutomationRun.workspace_id == context.workspace.id,
            AutomationRun.status.in_(("completed", "skipped", "waiting_confirmation")),
        )
        .order_by(AutomationRun.finished_at.desc().nullslast())
        .limit(1)
    )
    failed = await session.scalar(
        select(AutomationRun)
        .where(
            AutomationRun.workspace_id == context.workspace.id,
            AutomationRun.status == "failed",
        )
        .order_by(AutomationRun.finished_at.desc().nullslast())
        .limit(1)
    )
    heartbeat = await session.scalar(
        select(AutomationRun)
        .where(
            AutomationRun.workspace_id == context.workspace.id,
            AutomationRun.automation_type == "n8n_heartbeat",
            AutomationRun.status == "completed",
        )
        .order_by(AutomationRun.finished_at.desc().nullslast())
        .limit(1)
    )
    last_heartbeat = heartbeat.finished_at if heartbeat is not None else None
    if account is None:
        status: Literal["healthy", "stale", "not_configured"] = "not_configured"
    elif last_heartbeat is None or last_heartbeat < datetime.now(UTC) - timedelta(
        minutes=settings.n8n_heartbeat_stale_minutes
    ):
        status = "stale"
    else:
        status = "healthy"
    return AutomationStatusResponse(
        status=status,
        last_heartbeat_at=last_heartbeat,
        active_service_account=(
            await service_account_service.response(session, account)
            if account is not None
            else None
        ),
        recent_successful_run=(
            AutomationRunResponse.model_validate(successful) if successful is not None else None
        ),
        recent_failed_run=(
            AutomationRunResponse.model_validate(failed) if failed is not None else None
        ),
        stale_after_minutes=settings.n8n_heartbeat_stale_minutes,
    )


@router.get("/runs", response_model=AutomationRunPage)
async def automation_run_list(
    context: CurrentContext,
    session: DbSession,
    status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AutomationRunPage:
    items, total = await automation_runs.list_runs(
        session,
        context.workspace.id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return AutomationRunPage(
        items=[AutomationRunResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.get("/recurring-rules/due", response_model=RecurringDueResponse)
async def recurring_rules_due(
    context: RecurringReadContext,
    session: DbSession,
    limit: int = Query(default=100, ge=1, le=500),
) -> RecurringDueResponse:
    items = await recurring_service.due_rules(
        session, context.workspace_id, now=datetime.now(UTC), limit=limit
    )
    return RecurringDueResponse(
        items=[
            RecurringDueItem(rule_id=item.id, scheduled_for=item.next_run_at)
            for item in items
            if item.next_run_at is not None
        ]
    )


@router.post(
    "/recurring-rules/{rule_id}/execute",
    response_model=RecurringRuleExecutionResponse,
)
async def recurring_rule_execute(
    rule_id: uuid.UUID,
    data: RecurringExecuteRequest,
    context: RecurringExecuteContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> RecurringRuleExecutionResponse:
    rule = await session.get(RecurringRule, rule_id)
    if rule is None:
        raise ApiError(
            status_code=404,
            code="RECURRING_RULE_NOT_FOUND",
            message="Recurring rule was not found",
        )
    ensure_service_workspace(context, rule.workspace_id)
    execution, duplicate = await recurring_service.execute_rule(
        session,
        rule,
        scheduled_for=data.scheduled_for,
        idempotency_key=idempotency_key or "",
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        trigger_type="schedule",
    )
    return RecurringRuleExecutionResponse.model_validate(execution).model_copy(
        update={"duplicate": duplicate}
    )


@router.post("/reports/weekly", response_model=WeeklyReportResponse)
async def weekly_report(
    data: WeeklyReportRequest,
    context: ReportsContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> WeeklyReportResponse:
    ensure_service_workspace(context, data.workspace_id)
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=data.workspace_id,
        automation_type="weekly_report",
        trigger_type="schedule",
        idempotency_key=idempotency_key or "",
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        input_summary={"week_start": data.week_start.isoformat()},
    )
    if duplicate:
        if run.result_summary is None:
            raise ApiError(
                status_code=409,
                code="AUTOMATION_IDEMPOTENCY_CONFLICT",
                message="The previous weekly report is still running",
            )
        return WeeklyReportResponse.model_validate(run.result_summary).model_copy(
            update={"duplicate": True}
        )
    workspace = await _workspace(session, data.workspace_id)
    result = await reports.weekly_report(session, workspace, data.week_start)
    automation_runs.complete_run(run, result.model_dump(mode="json"))
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=None,
        entity_type="automation_run",
        entity_id=run.id,
        action="report.weekly.generate",
        before_data=None,
        after_data={"week_start": data.week_start.isoformat(), "status": "completed"},
        request_id=context.request_id,
        source="automation",
    )
    await session.commit()
    return result


@router.post("/reports/uncategorized", response_model=UncategorizedReportResponse)
async def uncategorized_report(
    data: ReportRequest,
    context: ReportsContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> UncategorizedReportResponse:
    ensure_service_workspace(context, data.workspace_id)
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=data.workspace_id,
        automation_type="uncategorized_report",
        trigger_type="schedule",
        idempotency_key=idempotency_key or "",
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        input_summary={},
    )
    if duplicate:
        if run.result_summary is None:
            raise ApiError(
                status_code=409,
                code="AUTOMATION_IDEMPOTENCY_CONFLICT",
                message="The previous uncategorized report is still running",
            )
        return UncategorizedReportResponse.model_validate(run.result_summary).model_copy(
            update={"duplicate": True}
        )
    workspace = await _workspace(session, data.workspace_id)
    result = await reports.uncategorized_report(session, workspace)
    automation_runs.complete_run(run, result.model_dump(mode="json"))
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=None,
        entity_type="automation_run",
        entity_id=run.id,
        action="report.uncategorized.generate",
        before_data=None,
        after_data={"count": result.count, "status": "completed"},
        request_id=context.request_id,
        source="automation",
    )
    await session.commit()
    return result


@router.get("/backup/status", response_model=BackupStatusResponse)
async def backup_status(
    context: BackupStatusContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> BackupStatusResponse:
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=context.workspace_id,
        automation_type="backup_status",
        trigger_type="schedule",
        idempotency_key=idempotency_key or "",
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        input_summary={},
    )
    if duplicate and run.result_summary is not None:
        return BackupStatusResponse.model_validate(run.result_summary)
    result = await get_backup_status(session)
    automation_runs.complete_run(run, result.model_dump(mode="json"))
    await session.commit()
    return result


@router.post(
    "/month-close/{year}/{month}/prepare",
    response_model=MonthClosureResponse,
)
async def automation_month_close_prepare(
    year: int,
    month: int,
    context: MonthClosePrepareContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> MonthClosureResponse:
    if context.workspace_id is None:
        raise ApiError(
            status_code=409,
            code="N8N_NOT_CONFIGURED",
            message="Month close automation requires a workspace-scoped service account",
        )
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=context.workspace_id,
        automation_type="month_close_prepare",
        trigger_type="schedule",
        idempotency_key=idempotency_key or "",
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        input_summary={"year": year, "month": month},
    )
    if duplicate:
        if run.result_summary is None:
            raise ApiError(
                status_code=409,
                code="AUTOMATION_IDEMPOTENCY_CONFLICT",
                message="The previous month close preparation is still running",
            )
        return MonthClosureResponse.model_validate(run.result_summary)
    workspace = await _workspace(session, context.workspace_id)
    closure = await month_close_service.prepare(
        session,
        workspace,
        month_close_service.period_date(year, month),
        actor_user_id=None,
        request_id=context.request_id,
        source="automation",
    )
    result = MonthClosureResponse.model_validate(closure)
    automation_runs.complete_run(run, result.model_dump(mode="json"))
    await session.commit()
    return result


@router.get("/notifications/targets", response_model=NotificationTargetResponse)
async def notification_targets(
    context: NotificationContext,
    session: DbSession,
    event_type: NotificationEventType,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> NotificationTargetResponse:
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=context.workspace_id,
        automation_type="notification_targets",
        trigger_type="schedule",
        idempotency_key=idempotency_key or "",
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        input_summary={"event_type": event_type},
    )
    if duplicate and run.result_summary is not None:
        return NotificationTargetResponse.model_validate(run.result_summary)
    filters = [
        NotificationSetting.event_type == event_type,
        NotificationSetting.channel == "telegram",
        NotificationSetting.enabled.is_(True),
        TelegramLink.status == "active",
    ]
    if context.workspace_id is not None:
        filters.append(NotificationSetting.workspace_id == context.workspace_id)
    rows = (
        await session.execute(
            select(
                NotificationSetting.workspace_id,
                NotificationSetting.user_id,
                TelegramLink.telegram_user_id,
                TelegramLink.telegram_chat_id,
            )
            .join(
                TelegramLink,
                (TelegramLink.user_id == NotificationSetting.user_id)
                & (TelegramLink.workspace_id == NotificationSetting.workspace_id),
            )
            .where(*filters)
            .order_by(NotificationSetting.workspace_id, NotificationSetting.user_id)
        )
    ).all()
    targets: dict[uuid.UUID, list[dict[str, object]]] = {}
    for workspace_id, user_id, telegram_user_id, chat_id in rows:
        targets.setdefault(workspace_id, []).append(
            {
                "user_id": str(user_id),
                "telegram_user_id": telegram_user_id,
                "telegram_chat_id": chat_id,
            }
        )
    result = NotificationTargetResponse(
        items=[
            NotificationTarget(
                workspace_id=workspace_id,
                event_type=event_type,
                recipients=recipients,
            )
            for workspace_id, recipients in targets.items()
        ]
    )
    automation_runs.complete_run(run, result.model_dump(mode="json"))
    await session.commit()
    return result


@router.post(
    "/notifications/pending/claim",
    response_model=PendingNotificationResponse,
)
async def pending_notifications_claim(
    context: NotificationContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> PendingNotificationResponse:
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=context.workspace_id,
        automation_type="notification_claim",
        trigger_type="schedule",
        idempotency_key=idempotency_key or "",
        service_account_id=context.service_account.id,
        initiated_by=None,
        request_id=context.request_id,
        input_summary={},
    )
    if duplicate:
        return PendingNotificationResponse(items=[], duplicate=True)
    now = datetime.now(UTC)
    filters = [
        TelegramIntent.intent_type == "notification_test",
        TelegramIntent.status == "pending",
        TelegramIntent.expires_at > now,
        (
            TelegramIntent.resolved_at.is_(None)
            | (TelegramIntent.resolved_at < now - timedelta(minutes=5))
        ),
        TelegramLink.status == "active",
    ]
    if context.workspace_id is not None:
        filters.append(TelegramIntent.workspace_id == context.workspace_id)
    rows = (
        await session.execute(
            select(TelegramIntent, TelegramLink.telegram_user_id)
            .join(
                TelegramLink,
                (TelegramLink.user_id == TelegramIntent.user_id)
                & (TelegramLink.workspace_id == TelegramIntent.workspace_id)
                & (TelegramLink.telegram_chat_id == TelegramIntent.telegram_chat_id),
            )
            .where(*filters)
            .order_by(TelegramIntent.created_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    ).all()
    items: list[PendingNotification] = []
    for intent, telegram_user_id in rows:
        intent.resolved_at = now
        message = str(intent.payload.get("message", ""))[:3900]
        if message:
            items.append(
                PendingNotification(
                    opaque_id=intent.opaque_id,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=intent.telegram_chat_id,
                    message=message,
                )
            )
    result = PendingNotificationResponse(items=items)
    automation_runs.complete_run(run, {"claimed": len(items)})
    await session.commit()
    return result


async def _workspace(session: DbSession, workspace_id: uuid.UUID) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.deleted_at is not None:
        raise ApiError(
            status_code=404,
            code="WORKSPACE_NOT_FOUND",
            message="Workspace was not found",
        )
    return workspace
