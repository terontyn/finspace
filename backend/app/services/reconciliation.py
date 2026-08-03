import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.google_sync import GoogleSheetBinding, SyncConflict, SyncOutbox, SyncRun
from app.db.models.transactions import FinancialTransaction
from app.dependencies.context import RequestContext
from app.integrations.google_client import GoogleApiError, GoogleClientProtocol
from app.services.audit import record_audit, request_uuid
from app.services.google_oauth import access_token, active_connection
from app.services.google_sheets import require_binding
from app.services.sync_hash import canonical_value, row_hash
from app.services.sync_payload import entity_payload

SyncEntity = Account | Category | FinancialTransaction

SHEET_LAYOUTS = {
    "transaction": ("'Операции'!A2:AA", 18, 19, 23, 24, 26),
    "account": ("'Счета'!A2:Q", 12, None, 13, 14, 16),
    "category": ("'Категории'!A2:P", 10, None, 12, 13, 15),
}


def _cell(row: list[Any], index: int) -> Any:
    return row[index] if len(row) > index else ""


def _sheet_resolution_payload(entity_type: str, row: list[Any], row_number: int) -> dict[str, Any]:
    """Keep both the raw snapshot and a backend-validated resolution payload."""
    if entity_type == "transaction":
        changed_fields = {
            "date": _cell(row, 0),
            "time": _cell(row, 1),
            "transaction_type": _cell(row, 2),
            "amount": _cell(row, 3),
            "currency": _cell(row, 4),
            "counterparty": _cell(row, 9),
            "description": _cell(row, 10),
            "comment": _cell(row, 11),
            "status": _cell(row, 12),
        }
        visible_row = {
            "_account_id": _cell(row, 20),
            "_target_account_id": _cell(row, 21),
            "_category_id": _cell(row, 22),
        }
        sheet_name = "Операции"
    elif entity_type == "account":
        changed_fields = {
            "name": _cell(row, 0),
            "account_type": _cell(row, 1),
            "institution": _cell(row, 3),
            "is_archived": _cell(row, 7),
        }
        visible_row = {}
        sheet_name = "Счета"
    else:
        changed_fields = {
            "name": _cell(row, 0),
            "category_type": _cell(row, 1),
            "parent": _cell(row, 2),
            "icon": _cell(row, 3),
            "color": _cell(row, 4),
            "sort_order": _cell(row, 5),
            "is_archived": _cell(row, 6),
        }
        visible_row = {}
        sheet_name = "Категории"
    return {
        "row": canonical_value(row),
        "row_number": row_number,
        "sheet_name": sheet_name,
        "changed_fields": canonical_value(changed_fields),
        "visible_row": canonical_value(visible_row),
    }


async def _database_entities(
    session: AsyncSession, workspace_id: uuid.UUID
) -> dict[str, dict[uuid.UUID, SyncEntity]]:
    transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace_id
                )
            )
        ).all()
    )
    accounts = list(
        (await session.scalars(select(Account).where(Account.workspace_id == workspace_id))).all()
    )
    categories = list(
        (await session.scalars(select(Category).where(Category.workspace_id == workspace_id))).all()
    )
    return {
        "transaction": {item.id: item for item in transactions},
        "account": {item.id: item for item in accounts},
        "category": {item.id: item for item in categories},
    }


async def _queue_restore(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    run: SyncRun,
    entity_type: str,
    entity: SyncEntity,
) -> None:
    event = SyncOutbox(
        workspace_id=binding.workspace_id,
        binding_id=binding.id,
        entity_type=entity_type,
        entity_id=entity.id,
        operation="upsert",
        entity_version=int(entity.version),
        payload=canonical_value(entity_payload(entity_type, entity)),
        idempotency_key=f"{binding.id}:reconcile:{run.id}:{entity_type}:{entity.id}",
        status="pending",
        available_at=datetime.now(UTC),
    )
    session.add(event)


async def reconcile(
    session: AsyncSession,
    client: GoogleClientProtocol,
    context: RequestContext,
) -> tuple[SyncRun, dict[str, int]]:
    binding = await require_binding(session, context.workspace.id)
    if binding.spreadsheet_id is None:
        raise ApiError(
            status_code=409,
            code="GOOGLE_SHEET_NOT_REGISTERED",
            message="Google Sheet is not registered",
        )
    spreadsheet_id = binding.spreadsheet_id
    if binding.status == "paused":
        raise ApiError(
            status_code=409, code="GOOGLE_SYNC_PAUSED", message="Synchronization is paused"
        )
    connection = await active_connection(session, context.workspace.id)
    if connection is None:
        raise ApiError(
            status_code=404,
            code="GOOGLE_CONNECTION_NOT_FOUND",
            message="Google connection was not found",
        )
    token = await access_token(session, client, connection)
    run = SyncRun(
        workspace_id=context.workspace.id,
        binding_id=binding.id,
        run_type="reconciliation",
        status="running",
        started_at=datetime.now(UTC),
        request_id=request_uuid(context.request_id),
        initiated_by=context.user.id,
    )
    session.add(run)
    await session.flush()
    database = await _database_entities(session, context.workspace.id)
    counts: Counter[str] = Counter()
    try:
        for entity_type, layout in SHEET_LAYOUTS.items():
            range_name, id_index, workspace_index, version_index, hash_index, deleted_index = layout
            rows = await client.get_values(token, spreadsheet_id, range_name)
            ids: list[uuid.UUID] = []
            parsed_rows: list[tuple[uuid.UUID, int, list[Any]]] = []
            for row_number, row in enumerate(rows, start=2):
                if len(row) <= id_index or not row[id_index]:
                    counts["unknown_in_sheet"] += 1
                    continue
                try:
                    entity_id = uuid.UUID(str(row[id_index]))
                except ValueError:
                    counts["invalid"] += 1
                    continue
                ids.append(entity_id)
                parsed_rows.append((entity_id, row_number, row))
            duplicates = {item for item, count in Counter(ids).items() if count > 1}
            seen: set[uuid.UUID] = set()
            for entity_id, row_number, row in parsed_rows:
                if entity_id in duplicates:
                    counts["duplicate_in_sheet"] += 1
                    continue
                seen.add(entity_id)
                entity = database[entity_type].get(entity_id)
                if entity is None:
                    counts["unknown_in_sheet"] += 1
                    continue
                if workspace_index is not None and (
                    len(row) <= workspace_index
                    or str(row[workspace_index]) != str(context.workspace.id)
                ):
                    counts["invalid"] += 1
                    continue
                try:
                    sheet_version = int(row[version_index])
                except (ValueError, TypeError, IndexError):
                    counts["invalid"] += 1
                    continue
                sheet_hash = str(row[hash_index]) if len(row) > hash_index else ""
                expected_hash = row_hash(entity_payload(entity_type, entity))
                database_version = int(entity.version)
                deleted_value = str(row[deleted_index]) if len(row) > deleted_index else ""
                expected_deleted = bool(entity.deleted_at)
                technical_changed = bool(deleted_value) != expected_deleted
                if (
                    sheet_version == database_version
                    and sheet_hash == expected_hash
                    and not technical_changed
                ):
                    counts["matched"] += 1
                    continue
                if database_version > sheet_version and not technical_changed:
                    counts["database_newer"] += 1
                    continue
                result = "sheet_newer" if sheet_version > database_version else "conflict"
                counts[result] += 1
                session.add(
                    SyncConflict(
                        workspace_id=context.workspace.id,
                        binding_id=binding.id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        database_version=database_version,
                        sheet_version=sheet_version,
                        database_payload=canonical_value(entity_payload(entity_type, entity)),
                        sheet_payload={
                            **_sheet_resolution_payload(entity_type, row, row_number),
                            "technical_changed": technical_changed,
                        },
                        conflicting_fields=["technical_fields"]
                        if technical_changed
                        else ["row_hash"],
                        status="open",
                    )
                )
            for missing_id in database[entity_type].keys() - seen:
                counts["missing_in_sheet"] += 1
                await _queue_restore(
                    session,
                    binding,
                    run,
                    entity_type,
                    database[entity_type][missing_id],
                )
    except GoogleApiError as exc:
        run.status = "failed"
        run.error_count = 1
        run.finished_at = datetime.now(UTC)
        run.summary = {"error_code": exc.code}
        await session.commit()
        raise ApiError(status_code=exc.status_code, code=exc.code, message=str(exc)) from exc
    run.status = "completed"
    run.processed_count = sum(counts.values())
    run.conflict_count = counts["conflict"] + counts["sheet_newer"]
    run.error_count = counts["invalid"]
    run.finished_at = datetime.now(UTC)
    run.summary = dict(counts)
    binding.last_reconciliation_at = run.finished_at
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="google_sheet_binding",
        entity_id=binding.id,
        action="sheet.reconcile",
        before_data=None,
        after_data=dict(counts),
        request_id=context.request_id,
    )
    await session.commit()
    return run, dict(counts)
