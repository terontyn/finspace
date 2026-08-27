"""Add the Goals aggregate and immutable contribution events.

Revision ID: 0010_goals
Revises: 0009_budget_planning_core
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_goals"
down_revision: str | None = "0009_budget_planning_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("target_amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("updated_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint("target_amount > 0", name="ck_goals_target_amount"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_goals_currency_format"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'cancelled')",
            name="ck_goals_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_goals_version"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_goals"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_goals_id_workspace"),
        sa.UniqueConstraint("id", "workspace_id", "currency", name="uq_goals_identity_currency"),
    )
    op.create_index(
        "ix_goals_workspace_lifecycle",
        "goals",
        ["workspace_id", "deleted_at", "status", "target_date", "created_at", "id"],
    )
    op.create_index(
        "ix_goals_workspace_currency", "goals", ["workspace_id", "currency", "deleted_at"]
    )

    op.create_table(
        "goal_contributions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("goal_id", UUID, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("contributed_at", TIMESTAMP, nullable=False),
        sa.Column("correction_of_id", UUID, nullable=True),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("request_id", UUID, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount != 0", name="ck_goal_contributions_amount_nonzero"),
        sa.CheckConstraint(
            "correction_of_id IS NOT NULL OR amount > 0",
            name="ck_goal_contributions_normal_positive",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_goal_contributions_currency_format"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["goal_id", "workspace_id", "currency"],
            ["goals.id", "goals.workspace_id", "goals.currency"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_goal_contributions"),
        sa.UniqueConstraint("id", "goal_id", "workspace_id", name="uq_goal_contributions_identity"),
    )
    op.create_foreign_key(
        "fk_goal_contributions_correction_identity",
        "goal_contributions",
        "goal_contributions",
        ["correction_of_id", "goal_id", "workspace_id"],
        ["id", "goal_id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_goal_contributions_goal_history",
        "goal_contributions",
        ["workspace_id", "goal_id", "contributed_at", "created_at", "id"],
    )
    op.create_index(
        "ix_goal_contributions_correction",
        "goal_contributions",
        ["workspace_id", "goal_id", "correction_of_id"],
    )

    op.create_table(
        "goal_command_results",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("goal_id", UUID, nullable=False),
        sa.Column("contribution_id", UUID, nullable=True),
        sa.Column("command_type", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_snapshot", JSONB, nullable=False),
        sa.Column("actor_user_id", UUID, nullable=False),
        sa.Column("request_id", UUID, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'", name="ck_goal_command_results_request_hash"
        ),
        sa.CheckConstraint(
            "response_status BETWEEN 200 AND 299",
            name="ck_goal_command_results_response_status",
        ),
        sa.CheckConstraint(
            "command_type IN ('create', 'update', 'pause', 'resume', 'complete', "
            "'reopen', 'cancel', 'delete', 'restore', 'contribution', 'correction')",
            name="ck_goal_command_results_command_type",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["goal_id", "workspace_id"],
            ["goals.id", "goals.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contribution_id", "goal_id", "workspace_id"],
            [
                "goal_contributions.id",
                "goal_contributions.goal_id",
                "goal_contributions.workspace_id",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_goal_command_results"),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_goal_command_results_workspace_key",
        ),
    )
    op.create_index(
        "ix_goal_command_results_goal_created",
        "goal_command_results",
        ["workspace_id", "goal_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("goal_command_results")
    op.drop_table("goal_contributions")
    op.drop_table("goals")
