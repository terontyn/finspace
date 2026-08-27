import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog

AUDIT_FIELDS: dict[str, tuple[str, ...]] = {
    "account": (
        "id",
        "workspace_id",
        "name",
        "account_type",
        "currency",
        "institution",
        "opening_balance",
        "opening_balance_at",
        "credit_limit",
        "description",
        "is_archived",
        "version",
        "deleted_at",
    ),
    "category": (
        "id",
        "workspace_id",
        "parent_id",
        "name",
        "category_type",
        "color",
        "icon",
        "sort_order",
        "is_archived",
        "version",
        "deleted_at",
    ),
    "transaction": (
        "id",
        "workspace_id",
        "occurred_at",
        "transaction_type",
        "amount",
        "currency",
        "account_id",
        "target_account_id",
        "category_id",
        "counterparty",
        "description",
        "comment",
        "status",
        "source",
        "related_transaction_id",
        "external_id",
        "import_batch_id",
        "version",
        "deleted_at",
    ),
    "recurring_rule": (
        "id",
        "workspace_id",
        "name",
        "rule_type",
        "schedule_rrule",
        "timezone",
        "transaction_type",
        "amount",
        "currency",
        "account_id",
        "target_account_id",
        "category_id",
        "creation_mode",
        "days_before_reminder",
        "is_active",
        "next_run_at",
        "last_run_at",
        "version",
        "deleted_at",
    ),
    "goal": (
        "id",
        "workspace_id",
        "name",
        "description",
        "currency",
        "target_amount",
        "target_date",
        "status",
        "version",
        "deleted_at",
    ),
}


def _json_value(value: object) -> Any:
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def snapshot(entity_type: str, entity: object) -> dict[str, Any]:
    return {field: _json_value(getattr(entity, field)) for field in AUDIT_FIELDS[entity_type]}


def request_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


async def record_audit(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    before_data: dict[str, Any] | None,
    after_data: dict[str, Any] | None,
    request_id: str,
    source: str = "api",
) -> AuditLog:
    entry = AuditLog(
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before_data=before_data,
        after_data=after_data,
        request_id=request_uuid(request_id),
        source=source,
    )
    session.add(entry)
    await session.flush()
    return entry


async def list_audit_entries(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
    offset: int,
) -> tuple[list[AuditLog], int]:
    filters = [AuditLog.workspace_id == workspace_id]
    if entity_type is not None:
        filters.append(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        filters.append(AuditLog.entity_id == entity_id)
    if date_from is not None:
        filters.append(AuditLog.created_at >= date_from)
    if date_to is not None:
        filters.append(AuditLog.created_at <= date_to)
    total = int(
        await session.scalar(select(func.count()).select_from(AuditLog).where(*filters)) or 0
    )
    entries = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(*filters)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return entries, total
