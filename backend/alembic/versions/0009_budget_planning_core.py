"""Add the Budget planning core.

Revision ID: 0009_budget_planning_core
Revises: 0008_month_close_invariants
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_budget_planning_core"
down_revision: str | None = "0008_month_close_invariants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMP = sa.DateTime(timezone=True)

STAGE6_AUDIT_ACTIONS = (
    "'create', 'update', 'archive', 'delete', 'restore', 'cancel', 'reconcile', "
    "'user.register', 'user.login', 'user.logout', 'auth.session.revoked', "
    "'import.upload', 'import.mapping', 'import.validate', 'import.commit', "
    "'import.rollback', 'backup.created', 'backup.verified', 'restore.verified', "
    "'google.connect', 'google.disconnect', 'google.revoke', 'sheet.create', "
    "'sheet.initialize', 'sheet.pause', 'sheet.resume', 'sheet.full_export', "
    "'sheet.reconcile', 'sheet.webhook_secret.rotate', 'sync.push', 'sync.pull', "
    "'sync.conflict', 'sync.conflict.resolve', 'sync.error', 'template.upgrade', "
    "'sheet.bridge.create', 'sheet.bridge.register', 'sheet.bridge.secret.rotate', "
    "'sheet.bridge.delete', 'sync.ack', 'sync.heartbeat', "
    "'service_account.create', 'service_account.key.rotate', 'service_account.revoke', "
    "'automation.run', 'recurring.create', 'recurring.update', 'recurring.execute', "
    "'recurring.pause', 'recurring.resume', 'telegram.link', 'telegram.unlink', "
    "'telegram.intent.create', 'telegram.intent.confirm', 'telegram.intent.cancel', "
    "'report.weekly.generate', 'report.uncategorized.generate', "
    "'month_close.prepare', 'month_close.confirm', 'month_close.reopen', "
    "'backup.remote.copy'"
)


def upgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action",
        "audit_log",
        f"action IN ({STAGE6_AUDIT_ACTIONS}, 'copy')",
    )
    op.create_table(
        "budget_periods",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("planned_income", sa.Numeric(20, 4), nullable=False),
        sa.Column("rollover_policy", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("updated_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "date_trunc('month', period_month)::date = period_month",
            name="ck_budget_periods_period_month",
        ),
        sa.CheckConstraint("planned_income >= 0", name="ck_budget_periods_planned_income"),
        sa.CheckConstraint("version >= 1", name="ck_budget_periods_version"),
        sa.CheckConstraint(
            "rollover_policy IN ('none', 'positive_only', 'full')",
            name="ck_budget_periods_rollover_policy",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_budget_periods"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_budget_periods_id_workspace"),
        sa.UniqueConstraint(
            "workspace_id", "period_month", "currency", name="uq_budget_periods_key"
        ),
    )
    op.create_index(
        "ix_budget_periods_workspace_period",
        "budget_periods",
        ["workspace_id", "period_month", "currency"],
    )

    op.create_table(
        "budget_allocations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("budget_period_id", UUID, nullable=False),
        sa.Column("category_id", UUID, nullable=False),
        sa.Column("planned_amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("planned_amount > 0", name="ck_budget_allocations_planned_amount"),
        sa.ForeignKeyConstraint(["budget_period_id"], ["budget_periods.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_budget_allocations"),
        sa.UniqueConstraint(
            "budget_period_id",
            "category_id",
            name="uq_budget_allocations_period_category",
        ),
    )
    op.create_index(
        "ix_budget_allocations_period_category",
        "budget_allocations",
        ["budget_period_id", "category_id"],
    )

    op.create_table(
        "budget_plan_revisions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("budget_period_id", UUID, nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("response_snapshot", JSONB, nullable=False),
        sa.Column("actor_user_id", UUID, nullable=False),
        sa.Column("request_id", UUID, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("revision_number >= 1", name="ck_budget_plan_revisions_revision_number"),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_budget_plan_revisions_request_hash",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["budget_period_id", "workspace_id"],
            ["budget_periods.id", "budget_periods.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_budget_plan_revisions"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_budget_plan_revisions_id_workspace"),
        sa.UniqueConstraint(
            "budget_period_id",
            "revision_number",
            name="uq_budget_plan_revisions_number",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_budget_plan_revisions_workspace_idempotency",
        ),
    )
    op.create_index(
        "ix_budget_plan_revisions_period_revision",
        "budget_plan_revisions",
        ["budget_period_id", "revision_number"],
    )


def downgrade() -> None:
    op.drop_table("budget_plan_revisions")
    op.drop_table("budget_allocations")
    op.drop_table("budget_periods")
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.execute(sa.text("DELETE FROM audit_log WHERE action = 'copy'"))
    op.create_check_constraint(
        "ck_audit_log_action", "audit_log", f"action IN ({STAGE6_AUDIT_ACTIONS})"
    )
