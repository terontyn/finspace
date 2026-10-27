"""Add persisted bulk categorization previews.

Revision ID: 0014_categorization_previews
Revises: 0013_categorization_rule_sets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_categorization_previews"
down_revision: str | None = "0013_categorization_rule_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TIMESTAMP = sa.DateTime(timezone=True)

STATUSES = (
    "'not_found', 'transfer', 'already_categorized', 'split', "
    "'reconciled', 'closed_period', 'matched', 'no_match'"
)
MODES = "'ids', 'filter'"


def upgrade() -> None:
    op.create_table(
        "categorization_previews",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("rule_set_version", sa.Integer(), nullable=False),
        sa.Column("selection_mode", sa.String(20), nullable=False),
        sa.Column("selection", JSONB, nullable=False),
        sa.Column("selected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("matched_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("no_match_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("transfer_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("already_categorized_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("split_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reconciled_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("closed_period_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("not_found_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.CheckConstraint(f"selection_mode IN ({MODES})", name="ck_categorization_previews_mode"),
        sa.CheckConstraint(
            "rule_set_version > 0", name="ck_categorization_previews_rule_set_version"
        ),
        sa.CheckConstraint("selected_count >= 0", name="ck_categorization_previews_selected"),
        sa.CheckConstraint("expires_at > created_at", name="ck_categorization_previews_expiry"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_categorization_previews"),
    )
    op.create_index(
        "ix_categorization_previews_workspace_created",
        "categorization_previews",
        ["workspace_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_categorization_previews_workspace_expiry",
        "categorization_previews",
        ["workspace_id", "expires_at"],
    )
    op.create_table(
        "categorization_preview_items",
        sa.Column("id", UUID, nullable=False),
        sa.Column("preview_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        # Deliberately no foreign keys on transaction_id, rule_id or category_id: the preview must
        # stay readable for its whole TTL even after the referenced rows are archived or deleted.
        sa.Column("transaction_id", UUID, nullable=False),
        sa.Column("transaction_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("transaction_snapshot", JSONB, nullable=True),
        sa.Column("rule_id", UUID, nullable=True),
        sa.Column("rule_version", sa.Integer(), nullable=True),
        sa.Column("rule_name", sa.String(200), nullable=True),
        sa.Column("category_id", UUID, nullable=True),
        sa.Column("category_version", sa.Integer(), nullable=True),
        sa.Column("category_name", sa.String(200), nullable=True),
        sa.CheckConstraint(
            f"status IN ({STATUSES})",
            name="ck_categorization_preview_items_status",
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_categorization_preview_items_sequence"),
        sa.ForeignKeyConstraint(["preview_id"], ["categorization_previews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_categorization_preview_items"),
        # The sequence unique constraint also provides the (preview_id, sequence) paging index.
        sa.UniqueConstraint(
            "preview_id", "sequence", name="uq_categorization_preview_items_sequence"
        ),
        sa.UniqueConstraint(
            "preview_id", "transaction_id", name="uq_categorization_preview_items_transaction"
        ),
    )


def downgrade() -> None:
    op.drop_table("categorization_preview_items")
    op.drop_index(
        "ix_categorization_previews_workspace_expiry", table_name="categorization_previews"
    )
    op.drop_index(
        "ix_categorization_previews_workspace_created", table_name="categorization_previews"
    )
    op.drop_table("categorization_previews")
