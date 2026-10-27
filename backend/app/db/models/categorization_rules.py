import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import SoftDeleteMixin, TimestampMixin, VersionMixin


class CategorizationRule(Base, TimestampMixin, VersionMixin, SoftDeleteMixin):
    __tablename__ = "categorization_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["payee_id", "workspace_id"],
            ["payees.id", "payees.workspace_id"],
            name="fk_categorization_rules_payee_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "workspace_id",
            name="uq_categorization_rules_id_workspace",
        ),
        CheckConstraint("version >= 1", name="ck_categorization_rules_version"),
        CheckConstraint("priority >= 0", name="ck_categorization_rules_priority"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_categorization_rules_name_not_empty"),
        CheckConstraint(
            "transaction_type IS NULL OR transaction_type IN "
            "('income', 'expense', 'refund', 'adjustment')",
            name="ck_categorization_rules_transaction_type",
        ),
        CheckConstraint(
            "counterparty_contains IS NULL OR length(btrim(counterparty_contains)) > 0",
            name="ck_categorization_rules_counterparty_not_empty",
        ),
        CheckConstraint(
            "description_contains IS NULL OR length(btrim(description_contains)) > 0",
            name="ck_categorization_rules_description_not_empty",
        ),
        CheckConstraint(
            "transaction_type IS NOT NULL OR account_id IS NOT NULL OR payee_id IS NOT NULL "
            "OR counterparty_contains IS NOT NULL OR description_contains IS NOT NULL",
            name="ck_categorization_rules_matcher_required",
        ),
        Index(
            "ix_categorization_rules_workspace_order",
            "workspace_id",
            "deleted_at",
            "is_active",
            "priority",
            "created_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        server_default="100",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    transaction_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True
    )
    payee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    counterparty_contains: Mapped[str | None] = mapped_column(String(300), nullable=True)
    description_contains: Mapped[str | None] = mapped_column(String(300), nullable=True)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
