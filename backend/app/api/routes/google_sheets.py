import uuid
from typing import Literal

from fastapi import APIRouter, Header, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.google_sync import GoogleSheetBinding, SyncRun
from app.db.models.users import User, Workspace
from app.dependencies.context import CurrentContext, EditorContext, OwnerContext, RequestContext
from app.dependencies.database import DbSession
from app.dependencies.google import GoogleClient
from app.schemas.common import PageMeta
from app.schemas.google import (
    AppsScriptAckRequest,
    AppsScriptAckResponse,
    AppsScriptBindingCreateResponse,
    AppsScriptBindingResponse,
    AppsScriptHeartbeatRequest,
    AppsScriptHeartbeatResponse,
    AppsScriptPackageResponse,
    AppsScriptPullRequest,
    AppsScriptPullResponse,
    AppsScriptPushRequest,
    AppsScriptPushResponse,
    AppsScriptPushResult,
    AppsScriptReconcileRequest,
    AppsScriptReconcileResponse,
    AppsScriptRegisterRequest,
    AppsScriptRegisterResponse,
    AppsScriptRotateSecretRequest,
    AppsScriptSecretResponse,
    ConflictPage,
    ConflictResolveRequest,
    ConflictResponse,
    FullExportPreview,
    FullExportRequest,
    GoogleActionResponse,
    GoogleSheetBindingResponse,
    GoogleSheetStatus,
    ReconciliationResponse,
    SyncRunPage,
    SyncRunResponse,
    WebhookChangeRequest,
    WebhookChangeResponse,
    WebhookPullResponse,
)
from app.services import apps_script_bridge, reconciliation, sync_conflicts, sync_webhook
from app.services import google_sheets as service

router = APIRouter()


def _validation_error(exc: ValidationError) -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Apps Script Bridge payload is invalid",
        details={"errors": exc.errors(include_input=False, include_url=False)},
    )


@router.get("/status", response_model=GoogleSheetStatus)
async def status(context: CurrentContext, session: DbSession) -> GoogleSheetStatus:
    return await service.sheet_status(session, context)


@router.post("/create", response_model=GoogleSheetBindingResponse, status_code=201)
async def create(
    context: OwnerContext,
    session: DbSession,
    client: GoogleClient,
) -> GoogleSheetBindingResponse:
    binding = await service.create_binding(session, client, context)
    return GoogleSheetBindingResponse.model_validate(binding)


@router.post("/initialize", response_model=SyncRunResponse)
async def initialize(
    context: OwnerContext,
    session: DbSession,
    client: GoogleClient,
) -> SyncRunResponse:
    run = await service.full_export(session, client, context, force=True)
    return SyncRunResponse.model_validate(run)


@router.post("/pause", response_model=GoogleSheetBindingResponse)
async def pause(context: OwnerContext, session: DbSession) -> GoogleSheetBindingResponse:
    return GoogleSheetBindingResponse.model_validate(
        await service.set_paused(session, context, paused=True)
    )


@router.post("/resume", response_model=GoogleSheetBindingResponse)
async def resume(context: OwnerContext, session: DbSession) -> GoogleSheetBindingResponse:
    return GoogleSheetBindingResponse.model_validate(
        await service.set_paused(session, context, paused=False)
    )


@router.get("/full-export-preview", response_model=FullExportPreview)
async def full_export_preview(context: EditorContext, session: DbSession) -> FullExportPreview:
    return await service.export_preview(session, context.workspace.id)


@router.post("/full-export", response_model=SyncRunResponse)
async def full_export(
    data: FullExportRequest,
    context: OwnerContext,
    session: DbSession,
    client: GoogleClient,
) -> SyncRunResponse:
    run = await service.full_export(session, client, context, force=data.force)
    return SyncRunResponse.model_validate(run)


@router.post("/reconcile", response_model=ReconciliationResponse)
async def reconcile(
    context: EditorContext,
    session: DbSession,
    client: GoogleClient,
) -> ReconciliationResponse:
    run, results = await reconciliation.reconcile(session, client, context)
    return ReconciliationResponse(run=SyncRunResponse.model_validate(run), results=results)


@router.get("/sync-runs", response_model=SyncRunPage)
async def sync_runs(
    context: CurrentContext,
    session: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SyncRunPage:
    filters = [SyncRun.workspace_id == context.workspace.id]
    total = int(
        await session.scalar(select(func.count()).select_from(SyncRun).where(*filters)) or 0
    )
    items = list(
        (
            await session.scalars(
                select(SyncRun)
                .where(*filters)
                .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return SyncRunPage(
        items=[SyncRunResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.get("/conflicts", response_model=ConflictPage)
async def conflicts(
    context: CurrentContext,
    session: DbSession,
    status: Literal["open", "resolved"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConflictPage:
    items, total = await sync_conflicts.list_conflicts(
        session,
        context.workspace.id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return ConflictPage(
        items=[ConflictResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.get("/conflicts/{conflict_id}", response_model=ConflictResponse)
async def conflict_get(
    conflict_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
) -> ConflictResponse:
    return ConflictResponse.model_validate(
        await sync_conflicts.get_conflict(session, context.workspace.id, conflict_id)
    )


@router.post("/conflicts/{conflict_id}/resolve", response_model=ConflictResponse)
async def conflict_resolve(
    conflict_id: uuid.UUID,
    data: ConflictResolveRequest,
    context: EditorContext,
    session: DbSession,
) -> ConflictResponse:
    return ConflictResponse.model_validate(
        await sync_conflicts.resolve_conflict(session, context, conflict_id, data)
    )


@router.post("/apps-script/secret", response_model=AppsScriptSecretResponse)
async def apps_script_secret(context: OwnerContext, session: DbSession) -> AppsScriptSecretResponse:
    binding, secret = await service.rotate_webhook_secret(session, context)
    base = (
        settings.public_webhook_base_url.rstrip("/") if settings.public_webhook_base_url else None
    )
    return AppsScriptSecretResponse(
        binding_id=binding.id,
        secret=secret,
        webhook_url=f"{base}/api/v1/google-sheets/webhook" if base else None,
        secret_version=binding.webhook_secret_version,
        warning="Секрет показан один раз. Сохраните его в Apps Script Properties.",  # noqa: RUF001
    )


@router.post(
    "/apps-script/binding",
    response_model=AppsScriptBindingCreateResponse,
    status_code=201,
)
async def apps_script_binding_create(
    context: OwnerContext,
    session: DbSession,
) -> AppsScriptBindingCreateResponse:
    return await apps_script_bridge.create_binding(session, context)


@router.get(
    "/apps-script/binding",
    response_model=AppsScriptBindingResponse,
)
async def apps_script_binding_get(
    context: CurrentContext,
    session: DbSession,
) -> AppsScriptBindingResponse:
    return await apps_script_bridge.get_binding(session, context)


@router.post(
    "/apps-script/binding/rotate-secret",
    response_model=AppsScriptBindingCreateResponse,
)
async def apps_script_binding_rotate(
    data: AppsScriptRotateSecretRequest,
    context: OwnerContext,
    session: DbSession,
) -> AppsScriptBindingCreateResponse:
    return await apps_script_bridge.rotate_secret(
        session,
        context,
        rebind=data.rebind,
    )


@router.post(
    "/apps-script/binding/pause",
    response_model=AppsScriptBindingResponse,
)
async def apps_script_binding_pause(
    context: OwnerContext,
    session: DbSession,
) -> AppsScriptBindingResponse:
    return await apps_script_bridge.set_paused(session, context, paused=True)


@router.post(
    "/apps-script/binding/resume",
    response_model=AppsScriptBindingResponse,
)
async def apps_script_binding_resume(
    context: OwnerContext,
    session: DbSession,
) -> AppsScriptBindingResponse:
    return await apps_script_bridge.set_paused(session, context, paused=False)


@router.delete("/apps-script/binding", response_model=GoogleActionResponse)
async def apps_script_binding_delete(
    context: OwnerContext,
    session: DbSession,
) -> GoogleActionResponse:
    await apps_script_bridge.delete_binding(session, context)
    return GoogleActionResponse(status="archived")


@router.get("/apps-script/package", response_model=AppsScriptPackageResponse)
async def apps_script_package(context: CurrentContext) -> AppsScriptPackageResponse:
    del context
    return apps_script_bridge.apps_script_package()


async def _bridge_request(
    request: Request,
    session: DbSession,
    binding_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    body_sha256: str | None,
    signature: str | None,
    *,
    allow_paused: bool = False,
) -> tuple[bytes, GoogleSheetBinding]:
    body = await request.body()
    binding = await apps_script_bridge.authenticate_request(
        session,
        binding_id=binding_id,
        timestamp=timestamp,
        nonce=nonce,
        body_sha256=body_sha256,
        signature=signature,
        body=body,
        allow_paused=allow_paused,
    )
    return body, binding


@router.post("/apps-script/register", response_model=AppsScriptRegisterResponse)
async def apps_script_register(
    request: Request,
    session: DbSession,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    body_sha256: str | None = Header(default=None, alias="X-Finspace-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> AppsScriptRegisterResponse:
    body, binding = await _bridge_request(
        request,
        session,
        binding_id,
        timestamp,
        nonce,
        body_sha256,
        signature,
    )
    try:
        data = AppsScriptRegisterRequest.model_validate_json(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return await apps_script_bridge.register(
        session,
        binding,
        data,
        request_id=str(getattr(request.state, "request_id", "")),
    )


@router.post("/apps-script/pull", response_model=AppsScriptPullResponse)
async def apps_script_pull(
    request: Request,
    session: DbSession,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    body_sha256: str | None = Header(default=None, alias="X-Finspace-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> AppsScriptPullResponse:
    body, binding = await _bridge_request(
        request,
        session,
        binding_id,
        timestamp,
        nonce,
        body_sha256,
        signature,
        allow_paused=True,
    )
    try:
        data = AppsScriptPullRequest.model_validate_json(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return await apps_script_bridge.pull(session, binding, data)


@router.post("/apps-script/ack", response_model=AppsScriptAckResponse)
async def apps_script_ack(
    request: Request,
    session: DbSession,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    body_sha256: str | None = Header(default=None, alias="X-Finspace-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> AppsScriptAckResponse:
    body, binding = await _bridge_request(
        request,
        session,
        binding_id,
        timestamp,
        nonce,
        body_sha256,
        signature,
    )
    try:
        data = AppsScriptAckRequest.model_validate_json(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return await apps_script_bridge.ack(session, binding, data)


@router.post("/apps-script/push", response_model=AppsScriptPushResponse)
async def apps_script_push(
    request: Request,
    session: DbSession,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    body_sha256: str | None = Header(default=None, alias="X-Finspace-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> AppsScriptPushResponse:
    body, binding = await _bridge_request(
        request,
        session,
        binding_id,
        timestamp,
        nonce,
        body_sha256,
        signature,
    )
    try:
        data = AppsScriptPushRequest.model_validate_json(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    results: list[AppsScriptPushResult] = []
    for event in data.events:
        try:
            result = await sync_webhook.apply_change(
                session,
                binding,
                event,
                request_id=str(getattr(request.state, "request_id", "")),
            )
            results.append(
                AppsScriptPushResult(
                    event_id=event.event_id,
                    status=result.status,
                    result=result,
                )
            )
        except ApiError as exc:
            results.append(
                AppsScriptPushResult(
                    event_id=event.event_id,
                    status="rejected",
                    error_code=exc.code,
                    error_message=exc.message,
                )
            )
    return AppsScriptPushResponse(results=results)


@router.post("/apps-script/heartbeat", response_model=AppsScriptHeartbeatResponse)
async def apps_script_heartbeat(
    request: Request,
    session: DbSession,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    body_sha256: str | None = Header(default=None, alias="X-Finspace-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> AppsScriptHeartbeatResponse:
    body, binding = await _bridge_request(
        request,
        session,
        binding_id,
        timestamp,
        nonce,
        body_sha256,
        signature,
        allow_paused=True,
    )
    try:
        data = AppsScriptHeartbeatRequest.model_validate_json(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return await apps_script_bridge.heartbeat(session, binding, data)


@router.post("/apps-script/reconcile", response_model=AppsScriptReconcileResponse)
async def apps_script_reconcile(
    request: Request,
    session: DbSession,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    body_sha256: str | None = Header(default=None, alias="X-Finspace-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> AppsScriptReconcileResponse:
    body, binding = await _bridge_request(
        request,
        session,
        binding_id,
        timestamp,
        nonce,
        body_sha256,
        signature,
    )
    try:
        data = AppsScriptReconcileRequest.model_validate_json(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return await apps_script_bridge.reconcile(
        session,
        binding,
        data,
        request_id=str(getattr(request.state, "request_id", "")),
    )


@router.delete("/binding", response_model=GoogleActionResponse)
async def binding_delete(context: OwnerContext, session: DbSession) -> GoogleActionResponse:
    await service.delete_binding(session, context)
    return GoogleActionResponse(status="archived")


async def _webhook_binding(
    request: Request,
    session: DbSession,
    binding_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
) -> tuple[bytes, GoogleSheetBinding]:
    body = await request.body()
    binding = await sync_webhook.authenticate_webhook(
        session,
        binding_id=binding_id,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        body=body,
    )
    return body, binding


@router.post("/webhook/changes", response_model=WebhookChangeResponse)
async def webhook_changes(
    request: Request,
    session: DbSession,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> WebhookChangeResponse:
    body, binding = await _webhook_binding(
        request, session, binding_id, timestamp, nonce, signature
    )
    try:
        payload = WebhookChangeRequest.model_validate_json(body)
    except ValidationError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="Webhook payload is invalid"
        ) from exc
    return await sync_webhook.apply_change(
        session,
        binding,
        payload,
        request_id=str(getattr(request.state, "request_id", "")),
    )


@router.post("/webhook/pull", response_model=WebhookPullResponse)
async def webhook_pull(
    request: Request,
    session: DbSession,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> WebhookPullResponse:
    _, binding = await _webhook_binding(request, session, binding_id, timestamp, nonce, signature)
    pending = int(
        await session.scalar(
            select(func.count())
            .select_from(SyncRun)
            .where(
                SyncRun.binding_id == binding.id,
                SyncRun.status == "running",
            )
        )
        or 0
    )
    return WebhookPullResponse(status="accepted", pending=pending)


@router.post("/webhook/reconcile", response_model=ReconciliationResponse)
async def webhook_reconcile(
    request: Request,
    session: DbSession,
    client: GoogleClient,
    binding_id: str | None = Header(default=None, alias="X-Finspace-Binding-ID"),
    timestamp: str | None = Header(default=None, alias="X-Finspace-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-Finspace-Nonce"),
    signature: str | None = Header(default=None, alias="X-Finspace-Signature"),
) -> ReconciliationResponse:
    _, binding = await _webhook_binding(request, session, binding_id, timestamp, nonce, signature)
    workspace = await session.get(Workspace, binding.workspace_id)
    user = await session.get(User, binding.created_by)
    if workspace is None or user is None:
        raise ApiError(status_code=500, code="INTERNAL_ERROR", message="Binding context is invalid")
    context = RequestContext(
        user=user,
        workspace=workspace,
        role="owner",
        request_id=str(getattr(request.state, "request_id", "")),
    )
    run, results = await reconciliation.reconcile(session, client, context)
    return ReconciliationResponse(run=SyncRunResponse.model_validate(run), results=results)
