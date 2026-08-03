"""Add authentication sessions and staging import.

Revision ID: 0003_auth_backup_import
Revises: 0002_financial_core
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_auth_backup_import"
down_revision: str | None = "0002_financial_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMP = sa.DateTime(timezone=True)

OLD_AUDIT_ACTIONS = "'create', 'update', 'archive', 'delete', 'restore', 'cancel', 'reconcile'"
NEW_AUDIT_ACTIONS = (
    OLD_AUDIT_ACTIONS + ", 'user.register', 'user.login', 'user.logout', 'auth.session.revoked', "
    "'import.upload', 'import.mapping', 'import.validate', 'import.commit', "
    "'import.rollback', 'backup.created', 'backup.verified', 'restore.verified'"
)
STAGE3_AUDIT_ACTIONS = (
    "'user.register', 'user.login', 'user.logout', 'auth.session.revoked', "
    "'import.upload', 'import.mapping', 'import.validate', 'import.commit', "
    "'import.rollback', 'backup.created', 'backup.verified', 'restore.verified'"
)


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(), nullable=True))
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("users", sa.Column("last_login_at", TIMESTAMP, nullable=True))
    op.add_column(
        "users",
        sa.Column("failed_login_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("users", sa.Column("locked_until", TIMESTAMP, nullable=True))
    op.create_check_constraint(
        "ck_users_failed_login_attempts",
        "users",
        "failed_login_attempts >= 0",
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("refresh_token_hash", sa.String(), nullable=False),
        sa.Column("user_agent_hash", sa.String(), nullable=True),
        sa.Column("ip_hash", sa.String(), nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.Column("replaced_by_session_id", UUID, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["replaced_by_session_id"],
            ["auth_sessions.id"],
            name="fk_auth_sessions_replaced_by_session_id_auth_sessions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.UniqueConstraint("refresh_token_hash", name="uq_auth_sessions_refresh_token_hash"),
    )
    op.create_index("ix_auth_sessions_user_expires", "auth_sessions", ["user_id", "expires_at"])
    op.create_index("ix_auth_sessions_active", "auth_sessions", ["user_id", "revoked_at"])

    op.create_table(
        "import_batches",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("stored_filename", sa.String(100), nullable=False),
        sa.Column("file_type", sa.String(20), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("detected_format", sa.String(100), nullable=True),
        sa.Column("mapping", postgresql.JSONB(), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("confirmed_at", TIMESTAMP, nullable=True),
        sa.Column("rolled_back_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("file_type IN ('csv', 'xlsx')", name="ck_import_batches_file_type"),
        sa.CheckConstraint("file_size > 0", name="ck_import_batches_file_size"),
        sa.CheckConstraint(
            "status IN ('uploaded', 'parsed', 'mapping_required', 'validated', 'ready', "
            "'importing', 'imported', 'failed', 'rolled_back', 'cancelled')",
            name="ck_import_batches_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_import_batches"),
    )
    op.create_index(
        "ix_import_batches_workspace_created",
        "import_batches",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_import_batches_workspace_hash",
        "import_batches",
        ["workspace_id", "file_sha256"],
    )
    op.create_index(
        "uq_import_batches_workspace_idempotency",
        "import_batches",
        ["workspace_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.add_column("transactions", sa.Column("import_batch_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_transactions_import_batch_id_import_batches",
        "transactions",
        "import_batches",
        ["import_batch_id"],
        ["id"],
    )
    op.create_index("ix_transactions_import_batch_id", "transactions", ["import_batch_id"])

    op.create_table(
        "import_rows",
        sa.Column("id", UUID, nullable=False),
        sa.Column("batch_id", UUID, nullable=False),
        sa.Column("source_sheet", sa.String(), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_data", postgresql.JSONB(), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=True),
        sa.Column("duplicate_transaction_id", UUID, nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_transaction_id", UUID, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source_row_number > 0", name="ck_import_rows_source_row_number"),
        sa.CheckConstraint(
            "status IN ('raw', 'valid', 'invalid', 'duplicate', 'skipped', 'imported', "
            "'rolled_back')",
            name="ck_import_rows_status",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["duplicate_transaction_id"], ["transactions.id"]),
        sa.ForeignKeyConstraint(["created_transaction_id"], ["transactions.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_import_rows"),
        sa.UniqueConstraint(
            "batch_id",
            "source_sheet",
            "source_row_number",
            name="uq_import_rows_batch_sheet_row",
        ),
    )
    op.create_index("ix_import_rows_batch_status", "import_rows", ["batch_id", "status"])

    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action",
        "audit_log",
        f"action IN ({NEW_AUDIT_ACTIONS})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.execute(sa.text(f"DELETE FROM audit_log WHERE action IN ({STAGE3_AUDIT_ACTIONS})"))
    op.create_check_constraint(
        "ck_audit_log_action",
        "audit_log",
        f"action IN ({OLD_AUDIT_ACTIONS})",
    )
    op.drop_table("import_rows")
    op.drop_index("ix_transactions_import_batch_id", table_name="transactions")
    op.drop_constraint(
        "fk_transactions_import_batch_id_import_batches",
        "transactions",
        type_="foreignkey",
    )
    op.drop_column("transactions", "import_batch_id")
    op.drop_table("import_batches")
    op.drop_table("auth_sessions")
    op.drop_constraint("ck_users_failed_login_attempts", "users", type_="check")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_attempts")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "email_verified")
    op.drop_column("users", "password_hash")
