"""Create the financial core domain schema.

Revision ID: 0002_financial_core
Revises: 0001_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_financial_core"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
MONEY = sa.Numeric(20, 4)
TIMESTAMP = sa.DateTime(timezone=True)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
    ]


def _editable_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID, nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("normalized_email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("locale", sa.String(20), server_default="ru-RU", nullable=False),
        sa.Column("timezone", sa.String(100), server_default="Europe/Amsterdam", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_editable_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("normalized_email", name=op.f("uq_users_normalized_email")),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("base_currency", sa.String(3), server_default="RUB", nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("owner_user_id", UUID, nullable=False),
        *_editable_columns(),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$'", name="ck_workspaces_base_currency_format"
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_workspaces_owner_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )

    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "role IN ('owner', 'editor', 'viewer')", name="ck_workspace_members_role"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_workspace_members_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_members_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name=op.f("pk_workspace_members")),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    op.create_table(
        "accounts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("account_type", sa.String(30), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("institution", sa.String(200), nullable=True),
        sa.Column("opening_balance", MONEY, server_default="0", nullable=False),
        sa.Column("opening_balance_at", TIMESTAMP, nullable=False),
        sa.Column("credit_limit", MONEY, nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_editable_columns(),
        sa.CheckConstraint(
            "account_type IN ('cash', 'debit_card', 'credit_card', 'current_account', "
            "'savings', 'deposit', 'brokerage', 'crypto_wallet', 'other')",
            name="ck_accounts_account_type",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_accounts_currency_format"),
        sa.CheckConstraint(
            "credit_limit IS NULL OR account_type = 'credit_card'",
            name="ck_accounts_credit_limit_type",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_accounts_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_index("ix_accounts_workspace_id", "accounts", ["workspace_id"])
    op.create_index("ix_accounts_workspace_archived", "accounts", ["workspace_id", "is_archived"])
    op.create_index(
        "uq_accounts_active_name",
        "accounts",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND is_archived = false"),
    )

    op.create_table(
        "categories",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("parent_id", UUID, nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category_type", sa.String(20), nullable=False),
        sa.Column("color", sa.String(20), nullable=True),
        sa.Column("icon", sa.String(50), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_editable_columns(),
        sa.CheckConstraint(
            "category_type IN ('income', 'expense', 'both')",
            name="ck_categories_category_type",
        ),
        sa.CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_categories_parent"),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["categories.id"], name=op.f("fk_categories_parent_id_categories")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_categories_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
    )
    op.create_index("ix_categories_workspace_id", "categories", ["workspace_id"])
    op.create_index("ix_categories_workspace_parent", "categories", ["workspace_id", "parent_id"])
    op.create_index("ix_categories_workspace_type", "categories", ["workspace_id", "category_type"])
    op.create_index(
        "uq_categories_root_name",
        "categories",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_categories_child_name",
        "categories",
        ["workspace_id", "parent_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NOT NULL AND deleted_at IS NULL"),
    )

    op.create_table(
        "transactions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("occurred_at", TIMESTAMP, nullable=False),
        sa.Column("transaction_type", sa.String(30), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("target_account_id", UUID, nullable=True),
        sa.Column("category_id", UUID, nullable=True),
        sa.Column("counterparty", sa.String(300), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), server_default="confirmed", nullable=False),
        sa.Column("source", sa.String(30), server_default="manual", nullable=False),
        sa.Column("related_transaction_id", UUID, nullable=True),
        sa.Column("external_id", sa.String(300), nullable=True),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_by", UUID, nullable=True),
        *_editable_columns(),
        sa.CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        sa.CheckConstraint(
            "account_id <> target_account_id", name="ck_transactions_distinct_accounts"
        ),
        sa.CheckConstraint(
            "transaction_type IN ('income', 'expense', 'transfer', 'refund', 'adjustment')",
            name="ck_transactions_transaction_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'reconciled', 'cancelled')",
            name="ck_transactions_status",
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'api', 'import', 'system')", name="ck_transactions_source"
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_transactions_currency_format"),
        sa.CheckConstraint(
            "(transaction_type = 'transfer' AND target_account_id IS NOT NULL "
            "AND category_id IS NULL) OR "
            "(transaction_type <> 'transfer' AND target_account_id IS NULL)",
            name="ck_transactions_transfer_shape",
        ),
        sa.CheckConstraint(
            "transaction_type <> 'refund' OR related_transaction_id IS NOT NULL",
            name="ck_transactions_refund_relation",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_transactions_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_transactions_category_id_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_transactions_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["related_transaction_id"],
            ["transactions.id"],
            name=op.f("fk_transactions_related_transaction_id_transactions"),
        ),
        sa.ForeignKeyConstraint(
            ["target_account_id"],
            ["accounts.id"],
            name=op.f("fk_transactions_target_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_transactions_updated_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_transactions_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
    )
    op.create_index(
        "ix_transactions_workspace_occurred", "transactions", ["workspace_id", "occurred_at"]
    )
    op.create_index(
        "ix_transactions_workspace_account_occurred",
        "transactions",
        ["workspace_id", "account_id", "occurred_at"],
    )
    op.create_index(
        "ix_transactions_workspace_category_occurred",
        "transactions",
        ["workspace_id", "category_id", "occurred_at"],
    )
    op.create_index("ix_transactions_workspace_status", "transactions", ["workspace_id", "status"])
    op.create_index(
        "ix_transactions_related_transaction_id", "transactions", ["related_transaction_id"]
    )
    op.create_index(
        "uq_transactions_workspace_external_id",
        "transactions",
        ["workspace_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    op.create_table(
        "transaction_splits",
        sa.Column("id", UUID, nullable=False),
        sa.Column("transaction_id", UUID, nullable=False),
        sa.Column("category_id", UUID, nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("amount > 0", name="ck_transaction_splits_amount_positive"),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_transaction_splits_category_id_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_transaction_splits_transaction_id_transactions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transaction_splits")),
    )
    op.create_index(
        "ix_transaction_splits_transaction_id", "transaction_splits", ["transaction_id"]
    )

    op.create_table(
        "audit_log",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=True),
        sa.Column("actor_user_id", UUID, nullable=True),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("before_data", postgresql.JSONB(), nullable=True),
        sa.Column("after_data", postgresql.JSONB(), nullable=True),
        sa.Column("request_id", UUID, nullable=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "action IN ('create', 'update', 'archive', 'delete', 'restore', 'cancel', 'reconcile')",
            name="ck_audit_log_action",
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'api', 'import', 'system')", name="ck_audit_log_source"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_workspace_created", "audit_log", ["workspace_id", "created_at"])
    op.create_index(
        "ix_audit_log_entity_created",
        "audit_log",
        ["entity_type", "entity_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("transaction_splits")
    op.drop_table("transactions")
    op.drop_table("categories")
    op.drop_table("accounts")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.drop_table("users")
