"""Add canonical Payees and explicit Payee relations.

Revision ID: 0011_payees
Revises: 0010_goals
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_payees"
down_revision: str | None = "0010_goals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "payees",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("updated_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_payees_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_payees_workspace_id_workspaces"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_payees_created_by_users"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_payees_updated_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_payees"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_payees_id_workspace"),
    )
    op.create_index(
        "ix_payees_workspace_lifecycle_name",
        "payees",
        ["workspace_id", "deleted_at", "name", "id"],
    )

    op.create_table(
        "payee_aliases",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("payee_id", UUID, nullable=False),
        sa.Column("alias", sa.String(300), nullable=False),
        sa.Column("normalized_alias", sa.Text(), nullable=False),
        sa.Column("normalized_alias_hash", sa.CHAR(64), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "length(normalized_alias) > 0", name="ck_payee_aliases_normalized_not_empty"
        ),
        sa.CheckConstraint(
            "normalized_alias_hash ~ '^[0-9a-f]{64}$'",
            name="ck_payee_aliases_hash_format",
        ),
        sa.CheckConstraint(
            "NOT is_primary OR deleted_at IS NULL",
            name="ck_payee_aliases_primary_not_deleted",
        ),
        sa.ForeignKeyConstraint(
            ["payee_id", "workspace_id"],
            ["payees.id", "payees.workspace_id"],
            name="fk_payee_aliases_payee_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_payee_aliases_created_by_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payee_aliases"),
        sa.UniqueConstraint(
            "workspace_id",
            "normalized_alias_hash",
            name="uq_payee_aliases_workspace_hash",
        ),
    )
    op.create_index(
        "ix_payee_aliases_payee_lifecycle",
        "payee_aliases",
        ["workspace_id", "payee_id", "deleted_at", "is_primary", "id"],
    )
    op.create_index(
        "uq_payee_aliases_active_primary",
        "payee_aliases",
        ["payee_id"],
        unique=True,
        postgresql_where=sa.text("is_primary = true"),
    )

    op.add_column("transactions", sa.Column("payee_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_transactions_payee_workspace",
        "transactions",
        "payees",
        ["payee_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_transactions_workspace_payee_occurred",
        "transactions",
        ["workspace_id", "payee_id", "occurred_at"],
    )

    op.add_column("recurring_rules", sa.Column("payee_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_recurring_rules_payee_workspace",
        "recurring_rules",
        "payees",
        ["payee_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_recurring_rules_workspace_payee",
        "recurring_rules",
        ["workspace_id", "payee_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_recurring_rules_workspace_payee", table_name="recurring_rules")
    op.drop_constraint("fk_recurring_rules_payee_workspace", "recurring_rules", type_="foreignkey")
    op.drop_column("recurring_rules", "payee_id")

    op.drop_index("ix_transactions_workspace_payee_occurred", table_name="transactions")
    op.drop_constraint("fk_transactions_payee_workspace", "transactions", type_="foreignkey")
    op.drop_column("transactions", "payee_id")

    op.drop_table("payee_aliases")
    op.drop_table("payees")
