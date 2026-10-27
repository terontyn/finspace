import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.google_sync import SyncConflict
from app.db.models.transactions import FinancialTransaction
from app.dependencies.context import RequestContext
from app.schemas.google import ConflictResolveRequest, WebhookChangeRequest
from app.services.audit import record_audit
from app.services.financial_period_guard import get_or_create_control
from app.services.google_sheets import require_binding
from app.services.sync_outbox import enqueue_entity
from app.services.sync_webhook import apply_change

SyncEntity = Account | Category | FinancialTransaction


async def list_conflicts(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[SyncConflict], int]:
    filters = [SyncConflict.workspace_id == workspace_id]
    if status is not None:
        filters.append(SyncConflict.status == status)
    total = int(
        await session.scalar(select(func.count()).select_from(SyncConflict).where(*filters)) or 0
    )
    items = list(
        (
            await session.scalars(
                select(SyncConflict)
                .where(*filters)
                .order_by(SyncConflict.created_at.desc(), SyncConflict.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def get_conflict(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SyncConflict:
    query = select(SyncConflict).where(
        SyncConflict.id == conflict_id,
        SyncConflict.workspace_id == workspace_id,
    )
    if for_update:
        query = query.with_for_update()
    conflict = await session.scalar(query)
    if conflict is None:
        raise ApiError(
            status_code=404, code="GOOGLE_SYNC_CONFLICT", message="Conflict was not found"
        )
    return conflict


async def _entity(session: AsyncSession, conflict: SyncConflict) -> SyncEntity:
    entity: SyncEntity | None
    if conflict.entity_type == "transaction":
        entity = await session.scalar(
            select(FinancialTransaction)
            .where(
                FinancialTransaction.id == conflict.entity_id,
                FinancialTransaction.workspace_id == conflict.workspace_id,
            )
            .with_for_update()
        )
    elif conflict.entity_type == "account":
        entity = await session.scalar(
            select(Account)
            .where(
                Account.id == conflict.entity_id,
                Account.workspace_id == conflict.workspace_id,
            )
            .with_for_update()
        )
    elif conflict.entity_type == "category":
        entity = await session.scalar(
            select(Category)
            .where(
                Category.id == conflict.entity_id,
                Category.workspace_id == conflict.workspace_id,
            )
            .with_for_update()
        )
    else:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="Conflict entity is unsupported"
        )
    if entity is None:
        raise ApiError(
            status_code=404, code="GOOGLE_SYNC_CONFLICT", message="Conflict entity is missing"
        )
    return entity


async def resolve_conflict(
    session: AsyncSession,
    context: RequestContext,
    conflict_id: uuid.UUID,
    data: ConflictResolveRequest,
) -> SyncConflict:
    candidate = await get_conflict(session, context.workspace.id, conflict_id)
    if data.resolution != "keep_database" and candidate.entity_type in {"transaction", "account"}:
        # Financial conflict resolution participates in the same global lock
        # order as every other ledger write.  The shared account/transaction
        # services perform the actual closed-period assertion.
        await get_or_create_control(session, context.workspace.id, for_update=True)
    conflict = await get_conflict(
        session,
        context.workspace.id,
        conflict_id,
        for_update=True,
    )
    if conflict.status != "open":
        raise ApiError(
            status_code=409, code="GOOGLE_SYNC_CONFLICT", message="Conflict is already resolved"
        )
    entity = await _entity(session, conflict)
    if int(entity.version) != conflict.database_version:
        raise ApiError(
            status_code=409,
            code="GOOGLE_SYNC_CONFLICT_STALE",
            message="The entity changed after this conflict was created",
            details={"current_version": int(entity.version)},
        )
    resolved_payload = data.merged_payload
    if data.resolution == "keep_database":
        await enqueue_entity(
            session,
            workspace_id=context.workspace.id,
            entity_type=conflict.entity_type,
            entity=entity,
        )
        resolved_payload = conflict.database_payload
    else:
        original = conflict.sheet_payload
        changed_fields = (
            data.merged_payload
            if data.resolution == "manual_merge"
            else dict(original.get("changed_fields", {}))
        )
        if not changed_fields:
            raise ApiError(
                status_code=422, code="VALIDATION_ERROR", message="Resolved fields are empty"
            )
        binding = await require_binding(session, context.workspace.id)
        if binding.spreadsheet_id is None:
            raise ApiError(
                status_code=409,
                code="GOOGLE_SHEET_NOT_REGISTERED",
                message="Google Sheet is not registered",
            )
        payload = WebhookChangeRequest(
            event_id=f"resolve-{conflict.id}-{uuid.uuid4()}",
            spreadsheet_id=binding.spreadsheet_id,
            sheet_name=str(
                original.get(
                    "sheet_name",
                    {"transaction": "Операции", "account": "Счета", "category": "Категории"}[
                        conflict.entity_type
                    ],
                )
            ),
            row_number=int(original.get("row_number", 2)),
            entity_type=conflict.entity_type,  # type: ignore[arg-type]
            entity_id=conflict.entity_id,
            expected_version=int(entity.version),
            row_hash=None,
            changed_fields=changed_fields,
            visible_row=dict(original.get("visible_row", {})),
        )
        result = await apply_change(session, binding, payload, request_id=context.request_id)
        if result.status != "applied":
            raise ApiError(
                status_code=409,
                code="GOOGLE_SYNC_CONFLICT",
                message="Resolution created another conflict",
            )
        resolved_payload = result.normalized_row
    conflict.status = "resolved"
    conflict.resolution = data.resolution
    conflict.resolved_payload = resolved_payload
    conflict.resolved_at = datetime.now(UTC)
    conflict.resolved_by = context.user.id
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="sync_conflict",
        entity_id=conflict.id,
        action="sync.conflict.resolve",
        before_data=None,
        after_data={"resolution": data.resolution, "entity_id": str(conflict.entity_id)},
        request_id=context.request_id,
    )
    await session.commit()
    return conflict
