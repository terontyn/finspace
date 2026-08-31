"""Add workspace-scoped categorization rule-set concurrency control.

Revision ID: 0013_categorization_rule_sets
Revises: 0012_categorization_rules
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_categorization_rule_sets"
down_revision: str | None = "0012_categorization_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "categorization_rule_set_controls",
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_categorization_rule_set_controls_version"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", name="pk_categorization_rule_set_controls"),
    )
    # Existing workspaces must be usable immediately; new and concurrent first use is still covered
    # by the get_or_create insert-on-conflict path in the service layer.
    op.execute(
        sa.text(
            "INSERT INTO categorization_rule_set_controls (workspace_id, version) "
            "SELECT id, 1 FROM workspaces ON CONFLICT DO NOTHING"
        )
    )


def downgrade() -> None:
    op.drop_table("categorization_rule_set_controls")
