"""Create the account reconciliation bounded context.

Revision ID: 0007_account_reconciliation
Revises: 0006_automations_telegram
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_account_reconciliation"
down_revision: str | None = "0006_automations_telegram"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
MONEY = sa.Numeric(20, 4)
TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "account_reconciliations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("statement_balance", MONEY, nullable=False),
        sa.Column("calculated_balance", MONEY, nullable=False),
        sa.Column("difference", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), server_default="confirmed", nullable=False),
        sa.Column("preview_token", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("confirmed_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("confirmed_at", TIMESTAMP, nullable=False),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_account_reconciliations_currency_format"
        ),
        sa.CheckConstraint("status = 'confirmed'", name="ck_account_reconciliations_status"),
        sa.CheckConstraint("difference = 0", name="ck_account_reconciliations_exact_match"),
        sa.CheckConstraint(
            "account_version > 0", name="ck_account_reconciliations_account_version"
        ),
        sa.CheckConstraint("version > 0", name="ck_account_reconciliations_version"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_account_reconciliations"),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_account_reconciliations_workspace_idempotency",
        ),
    )
    op.create_index(
        "ix_account_reconciliations_account_statement",
        "account_reconciliations",
        ["workspace_id", "account_id", "statement_date"],
    )

    op.create_table(
        "account_reconciliation_items",
        sa.Column("reconciliation_id", UUID, nullable=False),
        sa.Column("transaction_id", UUID, nullable=False),
        sa.Column("transaction_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "transaction_version > 0",
            name="ck_account_reconciliation_items_transaction_version",
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"],
            ["account_reconciliations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint(
            "reconciliation_id",
            "transaction_id",
            name="pk_account_reconciliation_items",
        ),
    )
    op.create_index(
        "ix_account_reconciliation_items_transaction",
        "account_reconciliation_items",
        ["transaction_id"],
    )


def downgrade() -> None:
    op.drop_table("account_reconciliation_items")
    op.drop_table("account_reconciliations")
