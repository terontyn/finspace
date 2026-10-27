"""Add deterministic categorization rules.

Revision ID: 0012_categorization_rules
Revises: 0011_payees
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_categorization_rules"
down_revision: str | None = "0011_payees"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "categorization_rules",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("transaction_type", sa.String(20), nullable=True),
        sa.Column("account_id", UUID, nullable=True),
        sa.Column("payee_id", UUID, nullable=True),
        sa.Column("counterparty_contains", sa.String(300), nullable=True),
        sa.Column("description_contains", sa.String(300), nullable=True),
        sa.Column("category_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("updated_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_categorization_rules_version"),
        sa.CheckConstraint("priority >= 0", name="ck_categorization_rules_priority"),
        sa.CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_categorization_rules_name_not_empty",
        ),
        sa.CheckConstraint(
            "transaction_type IS NULL OR transaction_type IN "
            "('income', 'expense', 'refund', 'adjustment')",
            name="ck_categorization_rules_transaction_type",
        ),
        sa.CheckConstraint(
            "counterparty_contains IS NULL OR length(btrim(counterparty_contains)) > 0",
            name="ck_categorization_rules_counterparty_not_empty",
        ),
        sa.CheckConstraint(
            "description_contains IS NULL OR length(btrim(description_contains)) > 0",
            name="ck_categorization_rules_description_not_empty",
        ),
        sa.CheckConstraint(
            "transaction_type IS NOT NULL OR account_id IS NOT NULL OR payee_id IS NOT NULL "
            "OR counterparty_contains IS NOT NULL OR description_contains IS NOT NULL",
            name="ck_categorization_rules_matcher_required",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_categorization_rules_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_categorization_rules_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payee_id", "workspace_id"],
            ["payees.id", "payees.workspace_id"],
            name="fk_categorization_rules_payee_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_categorization_rules_category_id_categories",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_categorization_rules_created_by_users",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_categorization_rules_updated_by_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_categorization_rules"),
        sa.UniqueConstraint(
            "id",
            "workspace_id",
            name="uq_categorization_rules_id_workspace",
        ),
    )
    op.create_index(
        "ix_categorization_rules_workspace_order",
        "categorization_rules",
        ["workspace_id", "deleted_at", "is_active", "priority", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_categorization_rules_workspace_order", table_name="categorization_rules")
    op.drop_table("categorization_rules")
