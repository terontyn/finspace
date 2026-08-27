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


class Goal(Base, TimestampMixin, VersionMixin, SoftDeleteMixin):
    __tablename__ = "goals"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_goals_id_workspace"),
        UniqueConstraint("id", "workspace_id", "currency", name="uq_goals_identity_currency"),
        CheckConstraint("target_amount > 0", name="target_amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'cancelled')",
            name="status",
        ),
        CheckConstraint("version >= 1", name="version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    target_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class GoalContribution(Base):
    __tablename__ = "goal_contributions"
    __table_args__ = (
        UniqueConstraint("id", "goal_id", "workspace_id", name="uq_goal_contributions_identity"),
        ForeignKeyConstraint(
            ["goal_id", "workspace_id", "currency"],
            ["goals.id", "goals.workspace_id", "goals.currency"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["correction_of_id", "goal_id", "workspace_id"],
            [
                "goal_contributions.id",
                "goal_contributions.goal_id",
                "goal_contributions.workspace_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount != 0", name="amount_nonzero"),
        CheckConstraint("correction_of_id IS NOT NULL OR amount > 0", name="normal_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    contributed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correction_of_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoalCommandResult(Base):
    __tablename__ = "goal_command_results"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_goal_command_results_workspace_key"
        ),
        ForeignKeyConstraint(
            ["goal_id", "workspace_id"],
            ["goals.id", "goals.workspace_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["contribution_id", "goal_id", "workspace_id"],
            [
                "goal_contributions.id",
                "goal_contributions.goal_id",
                "goal_contributions.workspace_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash"),
        CheckConstraint("response_status BETWEEN 200 AND 299", name="response_status"),
        CheckConstraint(
            "command_type IN ('create', 'update', 'pause', 'resume', 'complete', "
            "'reopen', 'cancel', 'delete', 'restore', 'contribution', 'correction')",
            name="command_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    contribution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    command_type: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
