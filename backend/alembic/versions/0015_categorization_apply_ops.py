"""Add idempotent bulk categorization apply operations and per-item results.

Revision ID: 0015_categorization_apply_ops
Revises: 0014_categorization_previews

The identifier is abbreviated because Alembic's ``alembic_version.version_num`` column is
``VARCHAR(32)``; the unabbreviated name would be 36 characters and cannot be stamped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015_categorization_apply_ops"
down_revision: str | None = "0014_categorization_previews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMP = sa.DateTime(timezone=True)

RESULT_STATUSES = (
    "'applied', 'transaction_changed', 'rule_changed', 'category_changed', "
    "'already_categorized', 'split', 'transfer', 'reconciled', 'closed_period', "
    "'no_match', 'not_found', 'failed'"
)
OPERATION_STATUSES = "'in_progress', 'completed'"


def upgrade() -> None:
    op.create_table(
        "categorization_apply_operations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        # Deliberately no foreign key: pruning an expired preview must not destroy the idempotency
        # record that makes an interrupted request safely replayable.
        sa.Column("preview_id", UUID, nullable=False),
        sa.Column("actor_user_id", UUID, nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), server_default="in_progress", nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            f"status IN ({OPERATION_STATUSES})",
            name="ck_categorization_apply_operations_status",
        ),
        sa.CheckConstraint(
            "requested_count > 0", name="ck_categorization_apply_operations_requested"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_categorization_apply_operations"),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_categorization_apply_operations_idempotency",
        ),
    )
    op.create_index(
        "ix_categorization_apply_operations_workspace_preview",
        "categorization_apply_operations",
        ["workspace_id", "preview_id"],
    )
    op.create_table(
        "categorization_apply_results",
        sa.Column("id", UUID, nullable=False),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("transaction_id", UUID, nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            f"status IN ({RESULT_STATUSES})",
            name="ck_categorization_apply_results_status",
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_categorization_apply_results_sequence"),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["categorization_apply_operations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_categorization_apply_results"),
        # The item uniqueness is the exactly-once guard: a terminal result is inserted in the same
        # transaction as the mutation it describes.
        sa.UniqueConstraint("operation_id", "item_id", name="uq_categorization_apply_results_item"),
        sa.UniqueConstraint(
            "operation_id", "sequence", name="uq_categorization_apply_results_sequence"
        ),
    )


def downgrade() -> None:
    op.drop_table("categorization_apply_results")
    op.drop_index(
        "ix_categorization_apply_operations_workspace_preview",
        table_name="categorization_apply_operations",
    )
    op.drop_table("categorization_apply_operations")
