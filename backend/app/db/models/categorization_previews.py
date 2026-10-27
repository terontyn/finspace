import uuid
from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PREVIEW_ITEM_STATUSES = (
    "not_found",
    "transfer",
    "already_categorized",
    "split",
    "reconciled",
    "closed_period",
    "matched",
    "no_match",
)
PREVIEW_SELECTION_MODES = ("ids", "filter")

_STATUS_SQL = ", ".join(f"'{status}'" for status in PREVIEW_ITEM_STATUSES)
_MODE_SQL = ", ".join(f"'{mode}'" for mode in PREVIEW_SELECTION_MODES)


class CategorizationPreview(Base):
    """An immutable workspace-scoped snapshot of deterministic categorization proposals.

    The preview belongs to the workspace, not to ``created_by``: any current member may read it.
    ``rule_set_version`` is the Stage A1 rule-set revision captured under the shared rule-set lock
    while the items were built, so a later apply can tell whether the rule set has moved on.
    """

    __tablename__ = "categorization_previews"
    __table_args__ = (
        CheckConstraint(f"selection_mode IN ({_MODE_SQL})", name="ck_categorization_previews_mode"),
        CheckConstraint("rule_set_version > 0", name="ck_categorization_previews_rule_set_version"),
        CheckConstraint("selected_count >= 0", name="ck_categorization_previews_selected"),
        CheckConstraint("expires_at > created_at", name="ck_categorization_previews_expiry"),
        Index(
            "ix_categorization_previews_workspace_created",
            "workspace_id",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
        Index("ix_categorization_previews_workspace_expiry", "workspace_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    rule_set_version: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    selected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    no_match_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    transfer_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    already_categorized_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    split_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reconciled_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    closed_period_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    not_found_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CategorizationPreviewItem(Base):
    """One persisted proposal.

    ``transaction_id``, ``rule_id`` and ``category_id`` are deliberately plain columns without
    foreign keys: the preview must stay readable for its whole TTL even if the transaction, rule or
    category is later archived or deleted. Only the preview -> item relation cascades.
    """

    __tablename__ = "categorization_preview_items"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_SQL})",
            name="ck_categorization_preview_items_status",
        ),
        CheckConstraint("sequence >= 0", name="ck_categorization_preview_items_sequence"),
        UniqueConstraint(
            "preview_id",
            "sequence",
            name="uq_categorization_preview_items_sequence",
        ),
        UniqueConstraint(
            "preview_id", "transaction_id", name="uq_categorization_preview_items_transaction"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    preview_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categorization_previews.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    transaction_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    transaction_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    category_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
