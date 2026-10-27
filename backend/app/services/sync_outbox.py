import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.google_sync import GoogleSheetBinding, SyncOutbox
from app.db.models.transactions import FinancialTransaction
from app.services.sync_hash import canonical_value
from app.services.sync_payload import entity_payload

SyncEntity = Account | Category | FinancialTransaction


async def active_binding(
    session: AsyncSession, workspace_id: uuid.UUID
) -> GoogleSheetBinding | None:
    return await session.scalar(
        select(GoogleSheetBinding).where(
            GoogleSheetBinding.workspace_id == workspace_id,
            GoogleSheetBinding.deleted_at.is_(None),
            or_(
                and_(
                    GoogleSheetBinding.status == "active",
                    GoogleSheetBinding.sync_enabled.is_(True),
                    GoogleSheetBinding.sync_mode != "paused",
                ),
                and_(
                    GoogleSheetBinding.status == "paused",
                    GoogleSheetBinding.sync_mode == "paused",
                ),
                and_(
                    GoogleSheetBinding.provider == "apps_script_bridge",
                    GoogleSheetBinding.status.in_(("creating", "initializing")),
                    GoogleSheetBinding.sync_enabled.is_(True),
                    GoogleSheetBinding.sync_mode != "paused",
                ),
            ),
        )
    )


async def enqueue_entity(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    entity_type: str,
    entity: SyncEntity,
    operation: str = "upsert",
) -> SyncOutbox | None:
    binding = await active_binding(session, workspace_id)
    if binding is None:
        return None
    entity_id = entity.id
    version = int(entity.version)
    idempotency_key = f"{binding.id}:{entity_type}:{entity_id}:{version}:{operation}"
    existing = await session.scalar(
        select(SyncOutbox).where(SyncOutbox.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    event = SyncOutbox(
        workspace_id=workspace_id,
        binding_id=binding.id,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=operation,
        entity_version=version,
        payload=canonical_value(entity_payload(entity_type, entity)),
        idempotency_key=idempotency_key,
        status="pending",
        available_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return event
