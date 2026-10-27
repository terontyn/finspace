import asyncio
import logging
import random
import signal
import socket
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.core.logging import configure_logging
from app.db.models.google_sync import GoogleConnection, GoogleSheetBinding, SyncOutbox, SyncRun
from app.db.models.users import User, Workspace
from app.db.session import AsyncSessionFactory, engine
from app.dependencies.context import RequestContext
from app.integrations.google_client import GoogleApiError, GoogleRestClient
from app.services.audit import record_audit
from app.services.google_oauth import access_token
from app.services.google_sheets import _snapshot_rows

# Named explicitly, like the prune worker: under `python -m app.workers.sync_worker` the module
# runs as `__main__`, and `logger` is the field that identifies the process in the shared JSON
# schema, so deriving the name from `__name__` would label every production line "__main__".
logger = logging.getLogger("app.workers.sync_worker")
WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4()}"
STOP = asyncio.Event()
LAYOUTS = {
    "transaction": ("'Операции'!S2:S", "'Операции'!A", 18),
    "account": ("'Счета'!M2:M", "'Счета'!A", 12),
    "category": ("'Категории'!K2:K", "'Категории'!A", 10),
}


def _stop() -> None:
    STOP.set()


async def claim_batch(session: AsyncSession) -> list[SyncOutbox]:
    now = datetime.now(UTC)
    stale = now - timedelta(minutes=15)
    statement = (
        select(SyncOutbox)
        .join(GoogleSheetBinding, GoogleSheetBinding.id == SyncOutbox.binding_id)
        .where(
            GoogleSheetBinding.deleted_at.is_(None),
            GoogleSheetBinding.provider == "google_oauth",
            GoogleSheetBinding.status == "active",
            GoogleSheetBinding.sync_enabled.is_(True),
            GoogleSheetBinding.sync_mode != "paused",
            or_(
                and_(
                    SyncOutbox.status.in_(("pending", "retry")),
                    SyncOutbox.available_at <= now,
                ),
                and_(SyncOutbox.status == "processing", SyncOutbox.locked_at < stale),
            ),
        )
        .order_by(SyncOutbox.available_at, SyncOutbox.created_at, SyncOutbox.id)
        .limit(settings.google_sheets_worker_batch_size)
        .with_for_update(skip_locked=True)
    )
    events = list((await session.scalars(statement)).all())
    for event in events:
        event.status = "processing"
        event.locked_at = now
        event.locked_by = WORKER_ID
        event.attempt_count += 1
    await session.commit()
    return events


async def _context(session: AsyncSession, binding: GoogleSheetBinding) -> RequestContext:
    workspace = await session.get(Workspace, binding.workspace_id)
    user = await session.get(User, binding.created_by)
    if workspace is None or user is None:
        raise ApiError(
            status_code=500, code="GOOGLE_SYNC_EVENT_FAILED", message="Binding context is missing"
        )
    return RequestContext(user=user, workspace=workspace, role="owner", request_id="")


def _row_map(values: list[list[Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for offset, row in enumerate(values, start=2):
        if row and row[0]:
            result[str(row[0])] = offset
    return result


async def _push_binding(
    session: AsyncSession,
    client: GoogleRestClient,
    binding: GoogleSheetBinding,
    events: list[SyncOutbox],
) -> None:
    if binding.spreadsheet_id is None:
        raise ApiError(
            status_code=409,
            code="GOOGLE_SHEET_NOT_REGISTERED",
            message="Google Sheet is not registered",
        )
    spreadsheet_id = binding.spreadsheet_id
    connection = await session.get(GoogleConnection, binding.google_connection_id)
    if connection is None:
        raise ApiError(
            status_code=404,
            code="GOOGLE_CONNECTION_NOT_FOUND",
            message="Google connection is missing",
        )
    token = await access_token(session, client, connection)
    context = await _context(session, binding)
    transaction_rows, account_rows, category_rows, list_rows = await _snapshot_rows(
        session, context
    )
    snapshots = {
        "transaction": {str(row[18]): row for row in transaction_rows},
        "account": {str(row[12]): row for row in account_rows},
        "category": {str(row[10]): row for row in category_rows},
    }
    data: list[dict[str, Any]] = []
    latest: dict[tuple[str, uuid.UUID], SyncOutbox] = {}
    for event in events:
        key = (event.entity_type, event.entity_id)
        if key not in latest or latest[key].entity_version < event.entity_version:
            latest[key] = event
    for entity_type in {event.entity_type for event in latest.values()}:
        if entity_type not in LAYOUTS:
            continue
        id_range, row_prefix, _ = LAYOUTS[entity_type]
        existing = _row_map(await client.get_values(token, spreadsheet_id, id_range))
        next_row = max(existing.values(), default=1) + 1
        for event in sorted(
            (item for item in latest.values() if item.entity_type == entity_type),
            key=lambda item: (item.created_at, item.id),
        ):
            row = snapshots[entity_type].get(str(event.entity_id))
            if row is None:
                raise ApiError(
                    status_code=404,
                    code="GOOGLE_SYNC_EVENT_FAILED",
                    message="Synchronized entity no longer exists",
                )
            row_number = existing.get(str(event.entity_id))
            if row_number is None:
                row_number = next_row
                existing[str(event.entity_id)] = row_number
                next_row += 1
            data.append({"range": f"{row_prefix}{row_number}", "values": [row]})
    if any(event.entity_type in {"account", "category"} for event in events):
        await client.clear_values(token, spreadsheet_id, "'_lists'!A2:D")
        if list_rows:
            data.append({"range": "'_lists'!A2", "values": list_rows})
    if data:
        await client.values_batch_update(token, spreadsheet_id, data)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, GoogleApiError):
        return exc.retryable
    if isinstance(exc, ApiError):
        return exc.code in {
            "GOOGLE_API_RATE_LIMITED",
            "GOOGLE_SYNC_EVENT_FAILED",
            "GOOGLE_TOKEN_EXPIRED",
        }
    return False


async def _finish_group(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    events: list[SyncOutbox],
    error: Exception | None,
) -> None:
    now = datetime.now(UTC)
    run = SyncRun(
        workspace_id=binding.workspace_id,
        binding_id=binding.id,
        run_type="outbox_push",
        status="completed" if error is None else "failed",
        started_at=min(event.locked_at or now for event in events),
        finished_at=now,
        processed_count=len(events),
        updated_count=len(events) if error is None else 0,
        error_count=0 if error is None else len(events),
        summary=(
            {"event_ids": [str(event.id) for event in events]}
            if error is None
            else {"error_code": getattr(error, "code", "GOOGLE_SYNC_EVENT_FAILED")}
        ),
    )
    session.add(run)
    if error is None:
        for event in events:
            event.status = "completed"
            event.processed_at = now
            event.locked_at = None
            event.locked_by = None
            event.last_error_code = None
            event.last_error_message = None
        binding.last_push_at = now
        binding.last_successful_sync_at = now
        binding.last_error_code = None
        binding.last_error_message = None
        await record_audit(
            session,
            workspace_id=binding.workspace_id,
            actor_user_id=None,
            entity_type="google_sheet_binding",
            entity_id=binding.id,
            action="sync.push",
            before_data=None,
            after_data={"events": len(events)},
            request_id="",
            source="worker",
        )
    else:
        code = str(getattr(error, "code", "GOOGLE_SYNC_EVENT_FAILED"))
        retryable = _retryable(error)
        for event in events:
            can_retry = (
                retryable and event.attempt_count < settings.google_sheets_max_retry_attempts
            )
            event.status = "retry" if can_retry else "failed"
            if can_retry:
                delay = min(3600.0, 2 ** min(event.attempt_count, 10))
                event.available_at = now + timedelta(seconds=delay * random.uniform(0.75, 1.25))
            else:
                event.processed_at = now
            event.locked_at = None
            event.locked_by = None
            event.last_error_code = code
            event.last_error_message = str(error)[:1000]
        if not retryable:
            binding.status = "error"
        binding.last_error_code = code
        binding.last_error_message = str(error)[:1000]
    await session.commit()


async def process_once(client: GoogleRestClient) -> int:
    async with AsyncSessionFactory() as session:
        events = await claim_batch(session)
    if not events:
        return 0
    grouped: dict[uuid.UUID, list[SyncOutbox]] = defaultdict(list)
    for event in events:
        grouped[event.binding_id].append(event)
    for binding_id, group in grouped.items():
        async with AsyncSessionFactory() as session:
            binding = await session.get(GoogleSheetBinding, binding_id)
            current_events = list(
                (
                    await session.scalars(
                        select(SyncOutbox).where(
                            SyncOutbox.id.in_([event.id for event in group]),
                            SyncOutbox.status == "processing",
                            SyncOutbox.locked_by == WORKER_ID,
                        )
                    )
                ).all()
            )
            if binding is None or not current_events:
                continue
            error: Exception | None = None
            try:
                await _push_binding(session, client, binding, current_events)
            except (GoogleApiError, ApiError) as exc:
                # JsonFormatter emits a fixed payload and drops `extra=`, so the machine-readable
                # code has to live in the message text. Only safe identifiers are logged: the raw
                # Google error text can name the spreadsheet and its ranges, and it is already
                # persisted on the outbox row and the binding for the UI to show.
                logger.warning(
                    "google_sync_batch_failed "
                    f"workspace_id={binding.workspace_id} "
                    f"binding_id={binding.id} "
                    f"events={len(current_events)} "
                    f"code={getattr(exc, 'code', None)}"
                )
                error = exc
            await _finish_group(session, binding, current_events, error)
    return len(events)


async def run() -> None:
    if not settings.google_sheets_sync_enabled:
        logger.info("Google Sheets sync worker is disabled")
        return
    if not settings.google_is_configured:
        logger.info("Google OAuth provider is disabled or not configured; worker is idle")
    client_id = settings.google_client_id_value or "not-configured"
    client_secret = settings.google_client_secret_value or "not-configured"
    client = GoogleRestClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=settings.google_redirect_uri,
    )
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, _stop)
        except NotImplementedError:
            signal.signal(name, lambda *_: _stop())
    while not STOP.is_set():
        try:
            processed = await process_once(client) if settings.google_is_configured else 0
        except Exception:
            logger.exception("Unexpected sync worker failure")
            processed = 0
        if processed == 0:
            try:
                await asyncio.wait_for(
                    STOP.wait(), timeout=settings.google_sheets_worker_poll_seconds
                )
            except TimeoutError:
                pass
    await engine.dispose()


def main() -> None:
    """Process entrypoint: configure logging once, then run the daemon.

    Logging is configured here rather than at import or inside ``run``/``process_once`` so that
    importing this module, or calling ``run`` directly from a test, never touches the global
    logging state of the calling process.
    """
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
