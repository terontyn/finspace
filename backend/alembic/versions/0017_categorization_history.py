"""Index categorization apply operations for newest-first workspace history.

Revision ID: 0017_categorization_history
Revises: 0016_audit_cause
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_categorization_history"
down_revision: str | None = "0016_audit_cause"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_categorization_apply_operations_workspace_created"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "categorization_apply_operations",
        ["workspace_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="categorization_apply_operations")
