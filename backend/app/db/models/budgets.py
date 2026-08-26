import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import SoftDeleteMixin, TimestampMixin, VersionMixin


class BudgetPeriod(Base, TimestampMixin, VersionMixin, SoftDeleteMixin):
    __tablename__ = "budget_periods"
    __table_args__ = (
        UniqueConstraint("workspace_id", "period_month", "currency", name="uq_budget_periods_key"),
        UniqueConstraint("id", "workspace_id", name="uq_budget_periods_id_workspace"),
        CheckConstraint(
            "date_trunc('month', period_month)::date = period_month", name="period_month"
        ),
        CheckConstraint("planned_income >= 0", name="planned_income"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(
            "rollover_policy IN ('none', 'positive_only', 'full')",
            name="rollover_policy",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    planned_income: Mapped[Decimal] = mapped_column(
        Numeric(20, 4), nullable=False, default=Decimal("0")
    )
    rollover_policy: Mapped[str] = mapped_column(String(30), nullable=False, default="none")
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class BudgetAllocation(Base, TimestampMixin):
    __tablename__ = "budget_allocations"
    __table_args__ = (
        UniqueConstraint(
            "budget_period_id", "category_id", name="uq_budget_allocations_period_category"
        ),
        CheckConstraint("planned_amount > 0", name="planned_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    budget_period_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budget_periods.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    planned_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BudgetPlanRevision(Base):
    __tablename__ = "budget_plan_revisions"
    __table_args__ = (
        UniqueConstraint(
            "budget_period_id", "revision_number", name="uq_budget_plan_revisions_number"
        ),
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_budget_plan_revisions_workspace_idempotency",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_budget_plan_revisions_id_workspace"),
        ForeignKeyConstraint(
            ["budget_period_id", "workspace_id"],
            ["budget_periods.id", "budget_periods.workspace_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision_number >= 1", name="revision_number"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    budget_period_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    response_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
