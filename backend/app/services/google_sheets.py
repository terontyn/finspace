import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.google_sync import (
    GoogleSheetBinding,
    SyncConflict,
    SyncInbox,
    SyncOutbox,
    SyncRun,
)
from app.db.models.transactions import FinancialTransaction
from app.dependencies.context import RequestContext
from app.integrations.google_client import GoogleApiError, GoogleClientProtocol
from app.integrations.google_template import SHEET_NAMES, GoogleSheetTemplateV1
from app.schemas.google import (
    FullExportPreview,
    GoogleConnectionStatus,
    GoogleSheetStatus,
    SyncMode,
)
from app.services.audit import record_audit
from app.services.calculations import calculate_balances
from app.services.google_oauth import access_token, active_connection, latest_connection
from app.services.sync_payload import account_row, category_row, transaction_row


async def current_binding(
    session: AsyncSession, workspace_id: uuid.UUID
) -> GoogleSheetBinding | None:
    return await session.scalar(
        select(GoogleSheetBinding).where(
            GoogleSheetBinding.workspace_id == workspace_id,
            GoogleSheetBinding.deleted_at.is_(None),
        )
    )


async def require_binding(session: AsyncSession, workspace_id: uuid.UUID) -> GoogleSheetBinding:
    binding = await current_binding(session, workspace_id)
    if binding is None:
        raise ApiError(
            status_code=404,
            code="GOOGLE_SHEET_NOT_FOUND",
            message="Google Sheet binding was not found",
        )
    return binding


async def sheet_status(session: AsyncSession, context: RequestContext) -> GoogleSheetStatus:
    connection = await latest_connection(session, context.workspace.id)
    binding = await current_binding(session, context.workspace.id)
    connection_status = GoogleConnectionStatus(
        configured=settings.google_is_configured,
        connected=connection is not None and connection.status == "active",
        status=connection.status if connection else None,
        google_email=connection.google_email if connection else None,
        granted_scopes=connection.granted_scopes if connection else [],
        token_expires_at=connection.token_expires_at if connection else None,
    )
    counts = {"pending": 0, "inbox": 0, "failed": 0, "conflicts": 0}
    if binding is not None:
        counts["pending"] = int(
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
        counts["failed"] = int(
            await session.scalar(
                select(func.count())
                .select_from(SyncOutbox)
                .where(
                    SyncOutbox.binding_id == binding.id,
                    SyncOutbox.status == "failed",
                )
            )
            or 0
        )
        counts["inbox"] = int(
            await session.scalar(
                select(func.count())
                .select_from(SyncInbox)
                .where(
                    SyncInbox.binding_id == binding.id,
                    SyncInbox.status.in_(("received", "validated")),
                )
            )
            or 0
        )
        counts["conflicts"] = int(
            await session.scalar(
                select(func.count())
                .select_from(SyncConflict)
                .where(
                    SyncConflict.binding_id == binding.id,
                    SyncConflict.status == "open",
                )
            )
            or 0
        )
    heartbeat_cutoff = datetime.now(UTC).timestamp() - (
        settings.apps_script_heartbeat_ttl_minutes * 60
    )
    return GoogleSheetStatus(
        configured=settings.google_provider_is_configured,
        provider=settings.google_sync_provider,
        oauth_enabled=settings.google_oauth_enabled,
        apps_script_bridge_enabled=settings.apps_script_bridge_enabled,
        public_backend_url=settings.public_backend_url,
        connection=connection_status,
        binding_id=binding.id if binding else None,
        spreadsheet_id=binding.spreadsheet_id if binding else None,
        spreadsheet_url=binding.spreadsheet_url if binding else None,
        spreadsheet_name=binding.spreadsheet_name if binding else None,
        template_version=binding.template_version if binding else None,
        status=binding.status if binding else None,
        sync_enabled=binding.sync_enabled if binding else False,
        sync_mode=cast(SyncMode, binding.sync_mode) if binding else None,
        apps_script_enabled=binding.apps_script_enabled if binding else False,
        last_successful_sync_at=binding.last_successful_sync_at if binding else None,
        last_reconciliation_at=binding.last_reconciliation_at if binding else None,
        pending_outbox=counts["pending"],
        pending_inbox=counts["inbox"],
        failed_events=counts["failed"],
        open_conflicts=counts["conflicts"],
        last_error_code=binding.last_error_code if binding else None,
        last_error_message=(
            binding.last_error_message[:500] if binding and binding.last_error_message else None
        ),
        webhook_configured=bool(
            binding
            and (
                binding.binding_secret_hash
                if binding.provider == "apps_script_bridge"
                else binding.webhook_secret_hash
            )
        ),
        spreadsheet_registered=bool(binding and binding.spreadsheet_id),
        last_pull_at=binding.last_pull_at if binding else None,
        last_ack_at=binding.last_ack_at if binding else None,
        last_heartbeat_at=binding.last_heartbeat_at if binding else None,
        heartbeat_healthy=bool(
            binding
            and binding.last_heartbeat_at
            and binding.last_heartbeat_at.timestamp() >= heartbeat_cutoff
        ),
    )


def _sheet_ids(payload: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in payload.get("sheets", []):
        properties = item.get("properties", {}) if isinstance(item, dict) else {}
        if "title" in properties and "sheetId" in properties:
            result[str(properties["title"])] = int(properties["sheetId"])
    if set(result) != set(SHEET_NAMES):
        raise ApiError(
            status_code=502,
            code="GOOGLE_SHEET_TEMPLATE_INVALID",
            message="Google returned an incomplete spreadsheet structure",
        )
    return result


async def create_binding(
    session: AsyncSession,
    client: GoogleClientProtocol,
    context: RequestContext,
) -> GoogleSheetBinding:
    if await current_binding(session, context.workspace.id):
        raise ApiError(
            status_code=409,
            code="GOOGLE_SHEET_BINDING_EXISTS",
            message="The workspace already has a Google Sheet binding",
        )
    connection = await active_connection(session, context.workspace.id)
    if connection is None:
        raise ApiError(
            status_code=404,
            code="GOOGLE_CONNECTION_NOT_FOUND",
            message="Connect a Google account first",
        )
    token = await access_token(session, client, connection)
    title = f"Финпространство — {context.workspace.name}"
    try:
        created = await client.create_spreadsheet(
            token,
            title,
            SHEET_NAMES,
            context.workspace.timezone,
        )
    except GoogleApiError as exc:
        raise ApiError(status_code=exc.status_code, code=exc.code, message=str(exc)) from exc
    spreadsheet_id = str(created.get("spreadsheetId", ""))
    if not spreadsheet_id:
        raise ApiError(
            status_code=502,
            code="GOOGLE_SHEET_INITIALIZATION_FAILED",
            message="Google did not return a spreadsheet ID",
        )
    binding = GoogleSheetBinding(
        workspace_id=context.workspace.id,
        google_connection_id=connection.id,
        provider="google_oauth",
        spreadsheet_id=spreadsheet_id,
        spreadsheet_url=str(
            created.get(
                "spreadsheetUrl",
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            )
        ),
        spreadsheet_name=title,
        template_version=settings.google_sheets_template_version,
        status="initializing",
        sync_enabled=True,
        sync_mode="push_only",
        apps_script_enabled=False,
        binding_secret_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
        binding_secret_created_at=datetime.now(UTC),
        created_by=context.user.id,
    )
    session.add(binding)
    await session.flush()
    await session.commit()
    try:
        meta = {
            "template_version": str(settings.google_sheets_template_version),
            "workspace_id": str(context.workspace.id),
            "binding_id": str(binding.id),
            "spreadsheet_id": spreadsheet_id,
            "created_at": binding.created_at.isoformat(),
            "last_full_export_at": "",
            "last_reconciliation_at": "",
            "backend_environment": settings.environment,
            "schema_revision": "0005_apps_script_bridge",
            "apps_script_version": "1",
        }
        await GoogleSheetTemplateV1().initialize(
            client,
            token,
            spreadsheet_id,
            _sheet_ids(created),
            meta,
        )
        await full_export(session, client, context, binding=binding, token=token, force=True)
        binding.status = "active"
        binding.last_error_code = None
        binding.last_error_message = None
        await record_audit(
            session,
            workspace_id=context.workspace.id,
            actor_user_id=context.user.id,
            entity_type="google_sheet_binding",
            entity_id=binding.id,
            action="sheet.create",
            before_data=None,
            after_data={
                "spreadsheet_id": spreadsheet_id,
                "template_version": binding.template_version,
                "sync_mode": binding.sync_mode,
            },
            request_id=context.request_id,
        )
        await session.commit()
        return binding
    except (GoogleApiError, ApiError) as exc:
        binding.status = "error"
        binding.last_error_code = getattr(exc, "code", "GOOGLE_SHEET_INITIALIZATION_FAILED")
        binding.last_error_message = str(exc)[:1000]
        await session.commit()
        if isinstance(exc, ApiError):
            raise
        raise ApiError(
            status_code=502,
            code="GOOGLE_SHEET_INITIALIZATION_FAILED",
            message="Google Sheet initialization failed",
        ) from exc


async def export_preview(session: AsyncSession, workspace_id: uuid.UUID) -> FullExportPreview:
    binding = await require_binding(session, workspace_id)
    entity_models = (FinancialTransaction, Account, Category)
    values = []
    for model in entity_models:
        values.append(
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                )
                or 0
            )
        )
    pending = int(
        await session.scalar(
            select(func.count())
            .select_from(SyncInbox)
            .where(
                SyncInbox.binding_id == binding.id,
                SyncInbox.status.in_(("received", "validated")),
            )
        )
        or 0
    )
    conflicts = int(
        await session.scalar(
            select(func.count())
            .select_from(SyncConflict)
            .where(
                SyncConflict.binding_id == binding.id,
                SyncConflict.status == "open",
            )
        )
        or 0
    )
    return FullExportPreview(
        transactions=values[0],
        accounts=values[1],
        categories=values[2],
        pending_changes=pending,
        open_conflicts=conflicts,
        blocked=pending > 0,
        warning=(
            "Входящие изменения ожидают обработки; нужен force. PostgreSQL-записи не удаляются."
            if pending
            else "Строки книги будут нормализованы из PostgreSQL; данные PostgreSQL не удаляются."
        ),
    )


async def _snapshot_rows(
    session: AsyncSession, context: RequestContext
) -> tuple[list[list[Any]], list[list[Any]], list[list[Any]], list[list[Any]]]:
    accounts = list(
        (
            await session.scalars(
                select(Account)
                .where(Account.workspace_id == context.workspace.id)
                .order_by(Account.name, Account.id)
            )
        ).all()
    )
    categories = list(
        (
            await session.scalars(
                select(Category)
                .where(Category.workspace_id == context.workspace.id)
                .order_by(Category.sort_order, Category.name, Category.id)
            )
        ).all()
    )
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction)
                .where(FinancialTransaction.workspace_id == context.workspace.id)
                .order_by(FinancialTransaction.occurred_at, FinancialTransaction.id)
            )
        ).all()
    )
    account_names = {item.id: item.name for item in accounts}
    category_names = {item.id: item.name for item in categories}
    balances = {
        item.account_id: item.balance
        for item in await calculate_balances(session, context.workspace.id)
    }
    account_rows = [
        account_row(item, calculated_balance=balances.get(item.id)) for item in accounts
    ]
    category_rows = [
        category_row(
            item,
            parent_name=(
                category_names.get(item.parent_id) if item.parent_id is not None else None
            ),
        )
        for item in categories
    ]
    transaction_rows = [
        transaction_row(
            item,
            timezone_name=context.workspace.timezone,
            account_names=account_names,
            category_names=category_names,
            owner_name=context.user.display_name,
        )
        for item in transactions
    ]
    list_rows = [
        [account.name, str(account.id), "", ""]
        for account in accounts
        if account.deleted_at is None
    ]
    for index, category in enumerate(item for item in categories if item.deleted_at is None):
        while len(list_rows) <= index:
            list_rows.append(["", "", "", ""])
        list_rows[index][2:] = [category.name, str(category.id)]
    return transaction_rows, account_rows, category_rows, list_rows


async def full_export(
    session: AsyncSession,
    client: GoogleClientProtocol,
    context: RequestContext,
    *,
    binding: GoogleSheetBinding | None = None,
    token: str | None = None,
    force: bool = False,
) -> SyncRun:
    binding = binding or await require_binding(session, context.workspace.id)
    if binding.spreadsheet_id is None:
        raise ApiError(
            status_code=409,
            code="GOOGLE_SHEET_NOT_REGISTERED",
            message="Google Sheet is not registered",
        )
    spreadsheet_id = binding.spreadsheet_id
    preview = await export_preview(session, context.workspace.id)
    if preview.blocked and not force:
        raise ApiError(
            status_code=409,
            code="GOOGLE_FULL_EXPORT_BLOCKED",
            message="Incoming Google Sheet changes must be processed before full export",
            details=preview.model_dump(),
        )
    connection = await active_connection(session, context.workspace.id)
    if connection is None:
        raise ApiError(
            status_code=404,
            code="GOOGLE_CONNECTION_NOT_FOUND",
            message="Google connection was not found",
        )
    token = token or await access_token(session, client, connection)
    run = SyncRun(
        workspace_id=context.workspace.id,
        binding_id=binding.id,
        run_type="initial_export" if binding.status == "initializing" else "manual_push",
        status="running",
        started_at=datetime.now(UTC),
        request_id=uuid.UUID(context.request_id) if context.request_id else None,
        initiated_by=context.user.id,
    )
    session.add(run)
    await session.flush()
    transaction_rows, account_rows, category_rows, list_rows = await _snapshot_rows(
        session, context
    )
    try:
        for range_name in (
            "'Операции'!A2:AA",
            "'Счета'!A2:Q",
            "'Категории'!A2:P",
            "'_lists'!A2:D",
        ):
            await client.clear_values(token, spreadsheet_id, range_name)
        data = []
        for range_name, rows in (
            ("'Операции'!A2", transaction_rows),
            ("'Счета'!A2", account_rows),
            ("'Категории'!A2", category_rows),
            ("'_lists'!A2", list_rows),
        ):
            if rows:
                data.append({"range": range_name, "values": rows})
        data.append(
            {
                "range": "'_sync_meta'!B6:B7",
                "values": [
                    [datetime.now(UTC).isoformat()],
                    [
                        binding.last_reconciliation_at.isoformat()
                        if binding.last_reconciliation_at
                        else ""
                    ],
                ],
            }
        )
        await client.values_batch_update(token, spreadsheet_id, data)
    except GoogleApiError as exc:
        run.status = "failed"
        run.error_count = 1
        run.finished_at = datetime.now(UTC)
        run.summary = {"error_code": exc.code}
        binding.status = "error"
        binding.last_error_code = exc.code
        binding.last_error_message = str(exc)[:1000]
        await session.commit()
        raise ApiError(status_code=exc.status_code, code=exc.code, message=str(exc)) from exc
    run.status = "completed"
    run.processed_count = len(transaction_rows) + len(account_rows) + len(category_rows)
    run.updated_count = run.processed_count
    run.finished_at = datetime.now(UTC)
    run.summary = {
        "transactions": len(transaction_rows),
        "accounts": len(account_rows),
        "categories": len(category_rows),
        "idempotent_replace": True,
    }
    binding.last_push_at = run.finished_at
    binding.last_successful_sync_at = run.finished_at
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.full_export" if binding.status != "initializing" else "sheet.initialize",
        before_data=None,
        after_data=run.summary,
        request_id=context.request_id,
    )
    await session.commit()
    return run


async def set_paused(
    session: AsyncSession, context: RequestContext, *, paused: bool
) -> GoogleSheetBinding:
    binding = await require_binding(session, context.workspace.id)
    binding.status = "paused" if paused else "active"
    binding.sync_enabled = not paused
    binding.sync_mode = (
        "paused" if paused else ("bidirectional" if binding.apps_script_enabled else "push_only")
    )
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.pause" if paused else "sheet.resume",
        before_data=None,
        after_data={"sync_mode": binding.sync_mode},
        request_id=context.request_id,
    )
    await session.commit()
    return binding


async def rotate_webhook_secret(
    session: AsyncSession, context: RequestContext
) -> tuple[GoogleSheetBinding, str]:
    binding = await require_binding(session, context.workspace.id)
    secret = secrets.token_urlsafe(48)
    binding.webhook_secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    binding.webhook_secret_version += 1
    binding.webhook_secret_rotated_at = datetime.now(UTC)
    binding.apps_script_enabled = True
    binding.sync_mode = "bidirectional"
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.webhook_secret.rotate",
        before_data=None,
        after_data={"secret_version": binding.webhook_secret_version},
        request_id=context.request_id,
    )
    await session.commit()
    return binding, secret


async def delete_binding(session: AsyncSession, context: RequestContext) -> None:
    binding = await require_binding(session, context.workspace.id)
    binding.deleted_at = datetime.now(UTC)
    binding.status = "archived"
    binding.sync_enabled = False
    binding.sync_mode = "paused"
    await session.execute(
        update(SyncOutbox)
        .where(
            SyncOutbox.binding_id == binding.id,
            SyncOutbox.status.in_(("pending", "retry")),
        )
        .values(status="cancelled")
    )
    await session.commit()
