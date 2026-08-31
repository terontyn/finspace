import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

APPLY_RESULT_STATUSES = (
    "applied",
    "transaction_changed",
    "rule_changed",
    "category_changed",
    "already_categorized",
    "split",
    "transfer",
    "reconciled",
    "closed_period",
    "no_match",
    "not_found",
    "failed",
)
APPLY_OPERATION_STATUSES = ("in_progress", "completed")

_RESULT_STATUS_SQL = ", ".join(f"'{status}'" for status in APPLY_RESULT_STATUSES)
_OPERATION_STATUS_SQL = ", ".join(f"'{status}'" for status in APPLY_OPERATION_STATUSES)


class CategorizationApplyOperation(Base):
    """One idempotent bulk-apply request.

    ``preview_id`` is a plain column rather than a foreign key: preview TTL pruning must never
    destroy the idempotency evidence that lets an interrupted request be replayed safely. The row is
    created before any item is processed and completed once every requested item has a terminal
    result, so a process that dies mid-request leaves a resumable operation rather than an
    unrecoverable in-progress state.
    """

    __tablename__ = "categorization_apply_operations"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_OPERATION_STATUS_SQL})",
            name="ck_categorization_apply_operations_status",
        ),
        CheckConstraint("requested_count > 0", name="ck_categorization_apply_operations_requested"),
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_categorization_apply_operations_idempotency",
        ),
        Index(
            "ix_categorization_apply_operations_workspace_preview",
            "workspace_id",
            "preview_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    preview_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="in_progress")
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CategorizationApplyResult(Base):
    """The terminal outcome of one requested preview item.

    Written inside the very same per-item database transaction as the transaction mutation, so a
    committed mutation and its recorded result can never disagree. ``item_id`` and
    ``transaction_id`` are plain columns for the same reason as ``preview_id`` above: the result
    must stay replayable after the preview it came from is pruned.
    """

    __tablename__ = "categorization_apply_results"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_RESULT_STATUS_SQL})",
            name="ck_categorization_apply_results_status",
        ),
        CheckConstraint("sequence >= 0", name="ck_categorization_apply_results_sequence"),
        UniqueConstraint("operation_id", "item_id", name="uq_categorization_apply_results_item"),
        UniqueConstraint(
            "operation_id", "sequence", name="uq_categorization_apply_results_sequence"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categorization_apply_operations.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
