import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog

# Stable machine-readable cause vocabulary. Never store human prose here.
CAUSE_CATEGORIZATION_RULE = "categorization_rule"
CAUSE_SOURCE_SINGLE_APPLY = "single_apply"
CAUSE_SOURCE_BULK_APPLY = "bulk_apply"


@dataclass(frozen=True)
class AuditCause:
    """Why a mutation happened, in a form the audit log can store generically.

    ``cause_id`` is immutable UUID evidence: it is recorded, never resolved against a live row, so
    the audit entry stays readable after the causing entity is archived or hard-deleted.
    """

    cause_type: str
    cause_id: uuid.UUID
    metadata: dict[str, Any] = field(default_factory=dict)


def categorization_cause(
    rule_id: uuid.UUID,
    *,
    source: str,
    preview_id: uuid.UUID | None = None,
    operation_id: uuid.UUID | None = None,
) -> AuditCause:
    """Build the cause for a categorization-driven transaction mutation."""
    metadata: dict[str, Any] = {"source": source}
    if preview_id is not None:
        metadata["preview_id"] = str(preview_id)
    if operation_id is not None:
        metadata["operation_id"] = str(operation_id)
    return AuditCause(cause_type=CAUSE_CATEGORIZATION_RULE, cause_id=rule_id, metadata=metadata)


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
    "categorization_rule": (
        "id",
        "workspace_id",
        "name",
        "priority",
        "is_active",
        "transaction_type",
        "account_id",
        "payee_id",
        "counterparty_contains",
        "description_contains",
        "category_id",
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
        "payee_id",
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
        "payee_id",
        "counterparty",
        "description",
        "comment",
        "creation_mode",
        "days_before_reminder",
        "is_active",
        "next_run_at",
        "last_run_at",
        "version",
        "deleted_at",
    ),
    "payee": (
        "id",
        "workspace_id",
        "name",
        "notes",
        "version",
        "created_by",
        "updated_by",
        "deleted_at",
    ),
    "payee_alias": (
        "id",
        "workspace_id",
        "payee_id",
        "alias",
        "normalized_alias",
        "normalized_alias_hash",
        "is_primary",
        "created_by",
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
    cause: AuditCause | None = None,
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
        cause_type=None if cause is None else cause.cause_type,
        cause_id=None if cause is None else cause.cause_id,
        cause_metadata=None if cause is None else (cause.metadata or None),
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
