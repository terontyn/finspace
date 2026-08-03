import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from redis.asyncio import Redis
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.google_sync import (
    GoogleSheetBinding,
    SyncConflict,
    SyncOutbox,
    SyncRun,
)
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace
from app.dependencies.context import RequestContext
from app.schemas.google import (
    AppsScriptAckRequest,
    AppsScriptAckResponse,
    AppsScriptBindingCreateResponse,
    AppsScriptBindingResponse,
    AppsScriptHeartbeatRequest,
    AppsScriptHeartbeatResponse,
    AppsScriptPackageResponse,
    AppsScriptPullEvent,
    AppsScriptPullRequest,
    AppsScriptPullResponse,
    AppsScriptReconcileAction,
    AppsScriptReconcileRequest,
    AppsScriptReconcileResponse,
    AppsScriptRegisterRequest,
    AppsScriptRegisterResponse,
    SyncRunResponse,
)
from app.services.audit import record_audit
from app.services.google_sheets import _snapshot_rows, current_binding
from app.services.sync_hash import canonical_value, row_hash
from app.services.sync_payload import entity_payload

EntityType = Literal["transaction", "account", "category"]
SyncMode = Literal["push_only", "bidirectional", "paused"]
BridgeProvider = Literal["apps_script_bridge"]
BridgeAction = Literal["pull", "conflict", "ignore"]

BRIDGE_PROVIDER: BridgeProvider = "apps_script_bridge"
SHEET_NAMES: dict[EntityType, str] = {
    "transaction": "Операции",
    "account": "Счета",
    "category": "Категории",
}
ROW_ID_INDEXES = {"transaction": 18, "account": 12, "category": 10}
ROW_HASH_INDEXES = {"transaction": 24, "account": 14, "category": 13}


def _new_secret() -> tuple[str, str]:
    secret = secrets.token_urlsafe(48)
    return secret, hashlib.sha256(secret.encode()).hexdigest()


def _backend_url() -> str:
    value = str(settings.public_backend_url or "").rstrip("/")
    if not settings.apps_script_bridge_is_configured or not value:
        raise ApiError(
            status_code=503,
            code="APPS_SCRIPT_BRIDGE_NOT_CONFIGURED",
            message="Apps Script Bridge requires PUBLIC_BACKEND_URL",
        )
    return value


async def require_bridge_binding(
    session: AsyncSession, workspace_id: uuid.UUID
) -> GoogleSheetBinding:
    binding = await current_binding(session, workspace_id)
    if binding is None or binding.provider != BRIDGE_PROVIDER:
        raise ApiError(
            status_code=404,
            code="APPS_SCRIPT_BINDING_NOT_FOUND",
            message="Apps Script Bridge binding was not found",
        )
    return binding


def _binding_response(binding: GoogleSheetBinding) -> AppsScriptBindingResponse:
    return AppsScriptBindingResponse(
        id=binding.id,
        provider=BRIDGE_PROVIDER,
        spreadsheet_id=binding.spreadsheet_id,
        spreadsheet_url=binding.spreadsheet_url,
        spreadsheet_name=binding.spreadsheet_name,
        template_version=binding.template_version,
        status=binding.status,
        sync_enabled=binding.sync_enabled,
        sync_mode=cast(SyncMode, binding.sync_mode),
        secret_created_at=binding.binding_secret_created_at,
        secret_last_rotated_at=binding.binding_secret_last_rotated_at,
        last_pull_at=binding.last_pull_at,
        last_ack_at=binding.last_ack_at,
        last_heartbeat_at=binding.last_heartbeat_at,
        created_at=binding.created_at,
    )


async def create_binding(
    session: AsyncSession, context: RequestContext
) -> AppsScriptBindingCreateResponse:
    _backend_url()
    if not settings.apps_script_bridge_enabled:
        raise ApiError(
            status_code=503,
            code="APPS_SCRIPT_BRIDGE_DISABLED",
            message="Apps Script Bridge is disabled",
        )
    if await current_binding(session, context.workspace.id) is not None:
        raise ApiError(
            status_code=409,
            code="GOOGLE_SHEET_BINDING_EXISTS",
            message="The workspace already has a Google Sheet binding",
        )
    secret, secret_hash = _new_secret()
    now = datetime.now(UTC)
    binding = GoogleSheetBinding(
        workspace_id=context.workspace.id,
        google_connection_id=None,
        provider=BRIDGE_PROVIDER,
        spreadsheet_id=None,
        spreadsheet_url=None,
        spreadsheet_name=f"Финпространство — {context.workspace.name}",
        template_version=settings.google_sheets_template_version,
        status="creating",
        sync_enabled=True,
        sync_mode="bidirectional",
        apps_script_enabled=True,
        binding_secret_hash=secret_hash,
        binding_secret_created_at=now,
        created_by=context.user.id,
    )
    session.add(binding)
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.bridge.create",
        before_data=None,
        after_data={"provider": BRIDGE_PROVIDER, "status": binding.status},
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(binding)
    return AppsScriptBindingCreateResponse(
        **_binding_response(binding).model_dump(),
        secret=secret,
        backend_url=_backend_url(),
        warning="Секрет показан один раз. Сохраните его только в Document Properties.",  # noqa: RUF001
    )


async def get_binding(session: AsyncSession, context: RequestContext) -> AppsScriptBindingResponse:
    return _binding_response(await require_bridge_binding(session, context.workspace.id))


async def rotate_secret(
    session: AsyncSession,
    context: RequestContext,
    *,
    rebind: bool,
) -> AppsScriptBindingCreateResponse:
    binding = await require_bridge_binding(session, context.workspace.id)
    secret, secret_hash = _new_secret()
    now = datetime.now(UTC)
    binding.binding_secret_hash = secret_hash
    binding.binding_secret_last_rotated_at = now
    if rebind:
        binding.spreadsheet_id = None
        binding.spreadsheet_url = None
        binding.status = "creating"
        binding.last_pull_at = None
        binding.last_ack_at = None
        binding.last_heartbeat_at = None
        await session.execute(
            update(SyncOutbox)
            .where(
                SyncOutbox.binding_id == binding.id,
                SyncOutbox.status.in_(("pending", "retry", "processing")),
            )
            .values(
                status="cancelled",
                locked_at=None,
                locked_by=None,
                processed_at=now,
            )
        )
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.bridge.secret.rotate",
        before_data=None,
        after_data={"rebind": rebind},
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(binding)
    return AppsScriptBindingCreateResponse(
        **_binding_response(binding).model_dump(),
        secret=secret,
        backend_url=_backend_url(),
        warning="Предыдущий secret больше не действует.",
    )


async def set_paused(
    session: AsyncSession, context: RequestContext, *, paused: bool
) -> AppsScriptBindingResponse:
    binding = await require_bridge_binding(session, context.workspace.id)
    binding.status = "paused" if paused else ("active" if binding.spreadsheet_id else "creating")
    binding.sync_enabled = not paused
    binding.sync_mode = "paused" if paused else "bidirectional"
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.pause" if paused else "sheet.resume",
        before_data=None,
        after_data={"provider": BRIDGE_PROVIDER, "status": binding.status},
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(binding)
    return _binding_response(binding)


async def delete_binding(session: AsyncSession, context: RequestContext) -> None:
    binding = await require_bridge_binding(session, context.workspace.id)
    now = datetime.now(UTC)
    binding.deleted_at = now
    binding.status = "archived"
    binding.sync_enabled = False
    binding.sync_mode = "paused"
    await session.execute(
        update(SyncOutbox)
        .where(
            SyncOutbox.binding_id == binding.id,
            SyncOutbox.status.in_(("pending", "retry", "processing")),
        )
        .values(
            status="cancelled",
            locked_at=None,
            locked_by=None,
            processed_at=now,
        )
    )
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.bridge.delete",
        before_data=None,
        after_data={"status": "archived"},
        request_id=context.request_id,
    )
    await session.commit()


def apps_script_package() -> AppsScriptPackageResponse:
    root = Path("/app/google-apps-script")
    if not root.is_dir():
        raise ApiError(
            status_code=503,
            code="APPS_SCRIPT_PACKAGE_UNAVAILABLE",
            message="Apps Script source package is unavailable",
        )
    names = sorted(path.name for path in root.iterdir() if path.suffix in {".gs", ".json"})
    return AppsScriptPackageResponse(
        files={name: (root / name).read_text(encoding="utf-8") for name in names}
    )


async def authenticate_request(
    session: AsyncSession,
    *,
    binding_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    body_sha256: str | None,
    signature: str | None,
    body: bytes,
    allow_paused: bool = False,
) -> GoogleSheetBinding:
    if not settings.apps_script_bridge_enabled:
        raise ApiError(
            status_code=503,
            code="APPS_SCRIPT_BRIDGE_DISABLED",
            message="Apps Script Bridge is disabled",
        )
    if not all((binding_id, timestamp, nonce, body_sha256, signature)):
        raise ApiError(
            status_code=401,
            code="APPS_SCRIPT_SIGNATURE_INVALID",
            message="Bridge authentication headers are missing",
        )
    if not 8 <= len(str(nonce)) <= 200:
        raise ApiError(
            status_code=401,
            code="APPS_SCRIPT_SIGNATURE_INVALID",
            message="Bridge nonce is invalid",
        )
    try:
        parsed_binding_id = uuid.UUID(str(binding_id))
        parsed_timestamp = int(str(timestamp))
    except ValueError as exc:
        raise ApiError(
            status_code=401,
            code="APPS_SCRIPT_SIGNATURE_INVALID",
            message="Bridge authentication headers are invalid",
        ) from exc
    if (
        abs(int(time.time()) - parsed_timestamp)
        > settings.google_sheets_webhook_max_clock_skew_seconds
    ):
        raise ApiError(
            status_code=401,
            code="APPS_SCRIPT_REQUEST_EXPIRED",
            message="Bridge timestamp is outside the allowed window",
        )
    actual_body_hash = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual_body_hash, str(body_sha256).lower()):
        raise ApiError(
            status_code=401,
            code="APPS_SCRIPT_BODY_HASH_INVALID",
            message="Bridge body hash is invalid",
        )
    binding = await session.scalar(
        select(GoogleSheetBinding).where(
            GoogleSheetBinding.id == parsed_binding_id,
            GoogleSheetBinding.provider == BRIDGE_PROVIDER,
            GoogleSheetBinding.deleted_at.is_(None),
        )
    )
    if binding is None:
        raise ApiError(
            status_code=404,
            code="APPS_SCRIPT_BINDING_NOT_FOUND",
            message="Apps Script Bridge binding was not found",
        )
    if binding.status in {"archived", "disconnected", "error"}:
        raise ApiError(
            status_code=409,
            code="APPS_SCRIPT_BINDING_INACTIVE",
            message="Apps Script Bridge binding is inactive",
        )
    if (binding.status == "paused" or not binding.sync_enabled) and not allow_paused:
        raise ApiError(
            status_code=409,
            code="GOOGLE_SYNC_PAUSED",
            message="Synchronization is paused",
        )
    signed = f"{parsed_timestamp}\n{nonce}\n{actual_body_hash}".encode()
    expected = hmac.new(
        bytes.fromhex(binding.binding_secret_hash), signed, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature).lower()):
        raise ApiError(
            status_code=401,
            code="APPS_SCRIPT_SIGNATURE_INVALID",
            message="Bridge signature is invalid",
        )
    redis = Redis.from_url(settings.redis_url_value, decode_responses=True)
    try:
        accepted = await redis.set(
            f"apps-script:nonce:{binding.id}:{nonce}",
            "1",
            ex=settings.google_sheets_webhook_max_clock_skew_seconds * 2,
            nx=True,
        )
    finally:
        await redis.aclose()
    if not accepted:
        raise ApiError(
            status_code=409,
            code="APPS_SCRIPT_REPLAY_DETECTED",
            message="Bridge nonce has already been used",
        )
    return binding


def _assert_spreadsheet(binding: GoogleSheetBinding, spreadsheet_id: str) -> None:
    if binding.spreadsheet_id != spreadsheet_id:
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="Spreadsheet does not match binding",
        )


async def _all_entities(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[tuple[EntityType, Account | Category | FinancialTransaction]]:
    accounts = list(
        (await session.scalars(select(Account).where(Account.workspace_id == workspace_id))).all()
    )
    categories = list(
        (await session.scalars(select(Category).where(Category.workspace_id == workspace_id))).all()
    )
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace_id
                )
            )
        ).all()
    )
    return [
        *(("account", entity) for entity in accounts),
        *(("category", entity) for entity in categories),
        *(("transaction", entity) for entity in transactions),
    ]


async def register(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    data: AppsScriptRegisterRequest,
    *,
    request_id: str,
) -> AppsScriptRegisterResponse:
    if data.template_version != settings.google_sheets_template_version:
        raise ApiError(
            status_code=409,
            code="GOOGLE_SHEET_TEMPLATE_INVALID",
            message="Apps Script template version does not match backend",
        )
    if binding.spreadsheet_id is not None:
        if binding.spreadsheet_id != data.spreadsheet_id:
            raise ApiError(
                status_code=409,
                code="APPS_SCRIPT_REBIND_REQUIRED",
                message="Rotate the secret with rebind=true before using another spreadsheet",
            )
        run = await session.scalar(
            select(SyncRun)
            .where(
                SyncRun.binding_id == binding.id,
                SyncRun.run_type == "initial_export",
            )
            .order_by(SyncRun.started_at.desc())
        )
        event_count = int(
            await session.scalar(
                select(func.count())
                .select_from(SyncOutbox)
                .where(
                    SyncOutbox.binding_id == binding.id,
                    SyncOutbox.operation == "full_export",
                )
            )
            or 0
        )
        return AppsScriptRegisterResponse(
            status="already_registered",
            binding_id=binding.id,
            spreadsheet_id=data.spreadsheet_id,
            initial_export_events=event_count,
            initial_export_run_id=run.id if run else None,
        )
    collision = await session.scalar(
        select(GoogleSheetBinding).where(
            GoogleSheetBinding.spreadsheet_id == data.spreadsheet_id,
            GoogleSheetBinding.id != binding.id,
            GoogleSheetBinding.deleted_at.is_(None),
        )
    )
    if collision is not None:
        raise ApiError(
            status_code=409,
            code="APPS_SCRIPT_SPREADSHEET_ALREADY_BOUND",
            message="Spreadsheet is already bound to another workspace",
        )
    now = datetime.now(UTC)
    binding.spreadsheet_id = data.spreadsheet_id
    binding.spreadsheet_url = data.spreadsheet_url
    binding.status = "initializing"
    binding.apps_script_enabled = True
    binding.sync_enabled = True
    binding.sync_mode = "bidirectional"
    run = SyncRun(
        workspace_id=binding.workspace_id,
        binding_id=binding.id,
        run_type="initial_export",
        status="running",
        started_at=now,
        request_id=uuid.UUID(request_id) if request_id else None,
        initiated_by=binding.created_by,
    )
    session.add(run)
    await session.flush()
    entities = await _all_entities(session, binding.workspace_id)
    for entity_type, entity in entities:
        session.add(
            SyncOutbox(
                workspace_id=binding.workspace_id,
                binding_id=binding.id,
                entity_type=entity_type,
                entity_id=entity.id,
                operation="full_export",
                entity_version=int(entity.version),
                payload=canonical_value(entity_payload(entity_type, entity)),
                idempotency_key=(f"{binding.id}:initial:{run.id}:{entity_type}:{entity.id}"),
                status="pending",
                available_at=now,
            )
        )
    if not entities:
        run.status = "completed"
        run.finished_at = now
        run.summary = {"events": 0, "empty_export": True}
        binding.status = "active"
        binding.last_successful_sync_at = now
    await record_audit(
        session,
        workspace_id=binding.workspace_id,
        actor_user_id=None,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.bridge.register",
        before_data=None,
        after_data={
            "spreadsheet_id": data.spreadsheet_id,
            "template_version": data.template_version,
            "apps_script_version": data.apps_script_version,
            "initial_export_events": len(entities),
        },
        request_id=request_id,
        source="google_sheets",
    )
    await session.commit()
    return AppsScriptRegisterResponse(
        status="registered",
        binding_id=binding.id,
        spreadsheet_id=data.spreadsheet_id,
        initial_export_events=len(entities),
        initial_export_run_id=run.id,
    )


async def _binding_context(session: AsyncSession, binding: GoogleSheetBinding) -> RequestContext:
    workspace = await session.get(Workspace, binding.workspace_id)
    user = await session.get(User, binding.created_by)
    if workspace is None or user is None:
        raise ApiError(
            status_code=500,
            code="INTERNAL_ERROR",
            message="Binding context is invalid",
        )
    return RequestContext(user=user, workspace=workspace, role="owner", request_id="")


async def pull(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    data: AppsScriptPullRequest,
) -> AppsScriptPullResponse:
    _assert_spreadsheet(binding, data.spreadsheet_id)
    lease_seconds = settings.apps_script_heartbeat_ttl_minutes * 60
    if binding.status == "paused" or not binding.sync_enabled:
        return AppsScriptPullResponse(status="paused", events=[], lease_seconds=lease_seconds)
    now = datetime.now(UTC)
    stale = now - timedelta(seconds=lease_seconds)
    limit = min(data.limit or settings.apps_script_pull_batch_size, 500)
    statement = (
        select(SyncOutbox)
        .where(
            SyncOutbox.binding_id == binding.id,
            SyncOutbox.entity_type.in_(tuple(SHEET_NAMES)),
            or_(
                (SyncOutbox.status.in_(("pending", "retry")) & (SyncOutbox.available_at <= now)),
                ((SyncOutbox.status == "processing") & (SyncOutbox.locked_at < stale)),
            ),
        )
        .order_by(SyncOutbox.available_at, SyncOutbox.created_at, SyncOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    events = list((await session.scalars(statement)).all())
    for event in events:
        event.status = "processing"
        event.locked_at = now
        event.locked_by = f"apps-script:{binding.id}"
        event.attempt_count += 1
    context = await _binding_context(session, binding)
    transaction_rows, account_rows, category_rows, _ = await _snapshot_rows(session, context)
    row_maps: dict[str, dict[str, list[Any]]] = {}
    for entity_type, rows in (
        ("transaction", transaction_rows),
        ("account", account_rows),
        ("category", category_rows),
    ):
        id_index = ROW_ID_INDEXES[entity_type]
        row_maps[entity_type] = {str(row[id_index]): row for row in rows}
    response_events: list[AppsScriptPullEvent] = []
    for event in events:
        entity_type = cast(EntityType, event.entity_type)
        row = row_maps[entity_type].get(str(event.entity_id))
        if row is None:
            event.status = "failed"
            event.processed_at = now
            event.last_error_code = "SYNC_ENTITY_NOT_FOUND"
            event.locked_at = None
            event.locked_by = None
            continue
        response_events.append(
            AppsScriptPullEvent(
                event_id=event.id,
                entity_type=entity_type,
                entity_id=event.entity_id,
                operation=event.operation,
                version=event.entity_version,
                sheet_name=SHEET_NAMES[entity_type],
                row=row,
                row_hash=str(row[ROW_HASH_INDEXES[entity_type]]),
                leased_until=now + timedelta(seconds=lease_seconds),
            )
        )
    binding.last_pull_at = now
    await record_audit(
        session,
        workspace_id=binding.workspace_id,
        actor_user_id=None,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sync.pull",
        before_data=None,
        after_data={"leased": len(response_events)},
        request_id="",
        source="google_sheets",
    )
    await session.commit()
    return AppsScriptPullResponse(
        status="ok",
        events=response_events,
        lease_seconds=lease_seconds,
    )


async def ack(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    data: AppsScriptAckRequest,
) -> AppsScriptAckResponse:
    ids = [item.event_id for item in data.events]
    events = {
        item.id: item
        for item in (
            await session.scalars(
                select(SyncOutbox).where(
                    SyncOutbox.id.in_(ids),
                    SyncOutbox.binding_id == binding.id,
                )
            )
        ).all()
    }
    if len(events) != len(set(ids)):
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="ACK contains an event outside this binding",
        )
    now = datetime.now(UTC)
    applied = failed = duplicates = 0
    for item in data.events:
        event = events[item.event_id]
        if event.status == "completed":
            duplicates += 1
            continue
        if item.status == "applied":
            event.status = "completed"
            event.processed_at = now
            event.last_error_code = None
            event.last_error_message = None
            event.payload = {
                **event.payload,
                "ack": {
                    "row_number": item.row_number,
                    "row_hash": item.row_hash,
                },
            }
            applied += 1
        else:
            can_retry = event.attempt_count < settings.google_sheets_max_retry_attempts
            event.status = "retry" if can_retry else "failed"
            event.available_at = now + timedelta(seconds=min(3600, 2**event.attempt_count))
            event.processed_at = None if can_retry else now
            event.last_error_code = item.error_code or "APPS_SCRIPT_APPLY_FAILED"
            failed += 1
        event.locked_at = None
        event.locked_by = None
    binding.last_ack_at = now
    initial_run = await session.scalar(
        select(SyncRun)
        .where(
            SyncRun.binding_id == binding.id,
            SyncRun.run_type == "initial_export",
            SyncRun.status == "running",
        )
        .order_by(SyncRun.started_at.desc())
    )
    initial_completed = False
    if initial_run is not None:
        total = int(
            await session.scalar(
                select(func.count())
                .select_from(SyncOutbox)
                .where(
                    SyncOutbox.binding_id == binding.id,
                    SyncOutbox.operation == "full_export",
                    SyncOutbox.idempotency_key.like(f"{binding.id}:initial:{initial_run.id}:%"),
                )
            )
            or 0
        )
        completed = int(
            await session.scalar(
                select(func.count())
                .select_from(SyncOutbox)
                .where(
                    SyncOutbox.binding_id == binding.id,
                    SyncOutbox.operation == "full_export",
                    SyncOutbox.idempotency_key.like(f"{binding.id}:initial:{initial_run.id}:%"),
                    SyncOutbox.status == "completed",
                )
            )
            or 0
        )
        initial_run.processed_count = completed
        initial_run.updated_count = completed
        initial_run.summary = {"events": total, "acknowledged": completed}
        if completed == total:
            initial_run.status = "completed"
            initial_run.finished_at = now
            binding.status = "active"
            binding.last_successful_sync_at = now
            initial_completed = True
    await record_audit(
        session,
        workspace_id=binding.workspace_id,
        actor_user_id=None,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sync.ack",
        before_data=None,
        after_data={
            "applied": applied,
            "failed": failed,
            "duplicates": duplicates,
            "initial_export_completed": initial_completed,
        },
        request_id="",
        source="google_sheets",
    )
    await session.commit()
    return AppsScriptAckResponse(
        status="ok",
        applied=applied,
        failed=failed,
        duplicates=duplicates,
        initial_export_completed=initial_completed,
    )


async def heartbeat(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    data: AppsScriptHeartbeatRequest,
) -> AppsScriptHeartbeatResponse:
    _assert_spreadsheet(binding, data.spreadsheet_id)
    now = datetime.now(UTC)
    binding.last_heartbeat_at = now
    pending = int(
        await session.scalar(
            select(func.count())
            .select_from(SyncOutbox)
            .where(
                SyncOutbox.binding_id == binding.id,
                SyncOutbox.status.in_(("pending", "retry", "processing")),
            )
        )
        or 0
    )
    await record_audit(
        session,
        workspace_id=binding.workspace_id,
        actor_user_id=None,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sync.heartbeat",
        before_data=None,
        after_data={"apps_script_version": data.apps_script_version},
        request_id="",
        source="google_sheets",
    )
    await session.commit()
    return AppsScriptHeartbeatResponse(
        status="ok",
        server_time=now,
        binding_status=binding.status,
        pending_outbox=pending,
    )


async def _accumulate_snapshot(
    binding: GoogleSheetBinding, data: AppsScriptReconcileRequest
) -> list[dict[str, Any]] | None:
    key = f"apps-script:reconcile:{binding.id}:{data.snapshot_id}"
    redis = Redis.from_url(settings.redis_url_value, decode_responses=True)
    try:
        if data.items:
            await redis.rpush(
                key,
                *(item.model_dump_json() for item in data.items),
            )
        await redis.expire(key, settings.apps_script_heartbeat_ttl_minutes * 60 * 2)
        if not data.final:
            return None
        values = await redis.lrange(key, 0, -1)
        await redis.delete(key)
    finally:
        await redis.aclose()
    return [json.loads(value) for value in values]


async def reconcile(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    data: AppsScriptReconcileRequest,
    *,
    request_id: str,
) -> AppsScriptReconcileResponse:
    _assert_spreadsheet(binding, data.spreadsheet_id)
    accumulated = await _accumulate_snapshot(binding, data)
    if accumulated is None:
        return AppsScriptReconcileResponse(status="batch_accepted", accepted=len(data.items))
    now = datetime.now(UTC)
    run = SyncRun(
        workspace_id=binding.workspace_id,
        binding_id=binding.id,
        run_type="reconciliation",
        status="running",
        started_at=now,
        request_id=uuid.UUID(request_id) if request_id else None,
        initiated_by=binding.created_by,
    )
    session.add(run)
    await session.flush()
    database: dict[tuple[EntityType, uuid.UUID], Account | Category | FinancialTransaction] = {}
    for entity_type, entity in await _all_entities(session, binding.workspace_id):
        database[(entity_type, entity.id)] = entity
    keys = [
        (cast(EntityType, str(item["entity_type"])), uuid.UUID(str(item["entity_id"])))
        for item in accumulated
    ]
    duplicates = {key for key, count in Counter(keys).items() if count > 1}
    seen: set[tuple[str, uuid.UUID]] = set()
    counts: Counter[str] = Counter()
    actions: list[AppsScriptReconcileAction] = []
    for item, key in zip(accumulated, keys, strict=True):
        entity_type, entity_id = key
        if key in duplicates:
            counts["duplicate_in_sheet"] += 1
            actions.append(
                AppsScriptReconcileAction(
                    action="ignore",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    row_number=int(item["row_number"]),
                    reason="duplicate_in_sheet",
                )
            )
            continue
        seen.add(key)
        db_entity = database.get(key)
        if db_entity is None:
            counts["unknown_in_sheet"] += 1
            actions.append(
                AppsScriptReconcileAction(
                    action="ignore",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    row_number=int(item["row_number"]),
                    reason="unknown_in_sheet",
                )
            )
            continue
        expected_hash = row_hash(entity_payload(entity_type, db_entity))
        sheet_version = int(item["version"])
        database_version = int(db_entity.version)
        sync_status = str(item["sync_status"]).upper()
        if "TAMPER" in sync_status:
            result = "technical_tamper"
        elif sync_status in {"DIRTY", "PENDING", "ERROR"}:
            result = "conflict"
        elif sheet_version == database_version and item["row_hash"] == expected_hash:
            counts["matched"] += 1
            continue
        elif sheet_version > database_version:
            result = "sheet_newer"
        elif sheet_version < database_version:
            result = "database_newer"
        else:
            result = "conflict"
        counts[result] += 1
        if result in {"sheet_newer", "conflict"}:
            conflict = SyncConflict(
                workspace_id=binding.workspace_id,
                binding_id=binding.id,
                entity_type=entity_type,
                entity_id=entity_id,
                database_version=database_version,
                sheet_version=sheet_version,
                database_payload=canonical_value(entity_payload(entity_type, db_entity)),
                sheet_payload=canonical_value(item),
                conflicting_fields=["row_hash"],
                status="open",
            )
            session.add(conflict)
            action: BridgeAction = "conflict"
        else:
            action = "pull"
            session.add(
                SyncOutbox(
                    workspace_id=binding.workspace_id,
                    binding_id=binding.id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    operation="upsert",
                    entity_version=database_version,
                    payload=canonical_value(entity_payload(entity_type, db_entity)),
                    idempotency_key=(
                        f"{binding.id}:bridge-reconcile:{run.id}:{entity_type}:{entity_id}"
                    ),
                    status="pending",
                    available_at=now,
                )
            )
        actions.append(
            AppsScriptReconcileAction(
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                row_number=int(item["row_number"]),
                reason=result,
            )
        )
    for key, entity in database.items():
        if key in seen:
            continue
        entity_type, entity_id = key
        counts["missing_in_sheet"] += 1
        session.add(
            SyncOutbox(
                workspace_id=binding.workspace_id,
                binding_id=binding.id,
                entity_type=entity_type,
                entity_id=entity_id,
                operation="upsert",
                entity_version=int(entity.version),
                payload=canonical_value(entity_payload(entity_type, entity)),
                idempotency_key=(
                    f"{binding.id}:bridge-reconcile:{run.id}:{entity_type}:{entity_id}"
                ),
                status="pending",
                available_at=now,
            )
        )
        actions.append(
            AppsScriptReconcileAction(
                action="pull",
                entity_type=entity_type,
                entity_id=entity_id,
                reason="missing_in_sheet",
            )
        )
    run.status = "completed"
    run.finished_at = datetime.now(UTC)
    run.processed_count = sum(counts.values())
    run.conflict_count = counts["conflict"] + counts["sheet_newer"]
    run.summary = dict(counts)
    binding.last_reconciliation_at = run.finished_at
    await record_audit(
        session,
        workspace_id=binding.workspace_id,
        actor_user_id=None,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.reconcile",
        before_data=None,
        after_data=dict(counts),
        request_id=request_id,
        source="google_sheets",
    )
    await session.commit()
    return AppsScriptReconcileResponse(
        status="completed",
        accepted=len(accumulated),
        run=SyncRunResponse.model_validate(run),
        results=dict(counts),
        actions=actions,
    )
