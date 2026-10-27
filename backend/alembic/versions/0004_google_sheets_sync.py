"""Add Google Sheets OAuth, binding and synchronization queues.

Revision ID: 0004_google_sheets_sync
Revises: 0003_auth_backup_import
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_google_sheets_sync"
down_revision: str | None = "0003_auth_backup_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TIMESTAMP = sa.DateTime(timezone=True)

STAGE3_AUDIT_ACTIONS = (
    "'create', 'update', 'archive', 'delete', 'restore', 'cancel', 'reconcile', "
    "'user.register', 'user.login', 'user.logout', 'auth.session.revoked', "
    "'import.upload', 'import.mapping', 'import.validate', 'import.commit', "
    "'import.rollback', 'backup.created', 'backup.verified', 'restore.verified'"
)
STAGE4_ONLY_AUDIT_ACTIONS = (
    "'google.connect', 'google.disconnect', 'google.revoke', 'sheet.create', "
    "'sheet.initialize', 'sheet.pause', 'sheet.resume', 'sheet.full_export', "
    "'sheet.reconcile', 'sheet.webhook_secret.rotate', 'sync.push', 'sync.pull', "
    "'sync.conflict', 'sync.conflict.resolve', 'sync.error', 'template.upgrade'"
)
STAGE4_AUDIT_ACTIONS = STAGE3_AUDIT_ACTIONS + ", " + STAGE4_ONLY_AUDIT_ACTIONS


def upgrade() -> None:
    op.drop_constraint("ck_transactions_source", "transactions", type_="check")
    op.create_check_constraint(
        "ck_transactions_source",
        "transactions",
        "source IN ('manual', 'api', 'import', 'system', 'google_sheets')",
    )
    op.create_table(
        "google_connections",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("connected_by", UUID, nullable=False),
        sa.Column("google_subject", sa.String(255), nullable=False),
        sa.Column("google_email", sa.String(320), nullable=True),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("token_key_version", sa.Integer(), nullable=False),
        sa.Column("token_expires_at", TIMESTAMP, nullable=True),
        sa.Column("granted_scopes", JSONB, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'revoked', 'error', 'disconnected')",
            name="ck_google_connections_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["connected_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_google_connections"),
    )
    op.create_index(
        "uq_google_connections_active_workspace",
        "google_connections",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND revoked_at IS NULL"),
    )
    op.create_index(
        "ix_google_connections_workspace_subject",
        "google_connections",
        ["workspace_id", "google_subject"],
    )

    op.create_table(
        "google_oauth_flows",
        sa.Column("id", UUID, nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("pkce_verifier_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("used_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_google_oauth_flows"),
        sa.UniqueConstraint("state_hash", name="uq_google_oauth_flows_state_hash"),
    )
    op.create_index("ix_google_oauth_flows_expires_at", "google_oauth_flows", ["expires_at"])

    op.create_table(
        "google_sheet_bindings",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("google_connection_id", UUID, nullable=False),
        sa.Column("spreadsheet_id", sa.String(255), nullable=False),
        sa.Column("spreadsheet_url", sa.Text(), nullable=False),
        sa.Column("spreadsheet_name", sa.String(300), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("sync_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sync_mode", sa.String(30), server_default="push_only", nullable=False),
        sa.Column("apps_script_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("webhook_secret_hash", sa.String(64), nullable=True),
        sa.Column("webhook_secret_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("webhook_secret_rotated_at", TIMESTAMP, nullable=True),
        sa.Column("last_push_at", TIMESTAMP, nullable=True),
        sa.Column("last_pull_at", TIMESTAMP, nullable=True),
        sa.Column("last_reconciliation_at", TIMESTAMP, nullable=True),
        sa.Column("last_successful_sync_at", TIMESTAMP, nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "status IN ('creating', 'initializing', 'active', 'paused', 'error', "
            "'disconnected', 'archived')",
            name="ck_google_sheet_bindings_status",
        ),
        sa.CheckConstraint(
            "sync_mode IN ('push_only', 'bidirectional', 'paused')",
            name="ck_google_sheet_bindings_sync_mode",
        ),
        sa.CheckConstraint("template_version > 0", name="ck_google_sheet_bindings_template"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["google_connection_id"], ["google_connections.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_google_sheet_bindings"),
        sa.UniqueConstraint("spreadsheet_id", name="uq_google_sheet_bindings_spreadsheet_id"),
    )
    op.create_index(
        "uq_google_sheet_bindings_active_workspace",
        "google_sheet_bindings",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND status <> 'archived'"),
    )
    op.create_index(
        "ix_google_sheet_bindings_connection",
        "google_sheet_bindings",
        ["google_connection_id"],
    )

    op.create_table(
        "sync_outbox",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("binding_id", UUID, nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", TIMESTAMP, nullable=False),
        sa.Column("locked_at", TIMESTAMP, nullable=True),
        sa.Column("locked_by", sa.String(255), nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "entity_type IN ('account', 'category', 'transaction', 'binding')",
            name="ck_sync_outbox_entity_type",
        ),
        sa.CheckConstraint(
            "operation IN ('upsert', 'delete', 'archive', 'restore', 'full_export', "
            "'refresh_lists')",
            name="ck_sync_outbox_operation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'retry', 'failed', 'cancelled')",
            name="ck_sync_outbox_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_sync_outbox_attempt_count"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["binding_id"], ["google_sheet_bindings.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_sync_outbox"),
        sa.UniqueConstraint("idempotency_key", name="uq_sync_outbox_idempotency_key"),
    )
    op.create_index(
        "ix_sync_outbox_dispatch",
        "sync_outbox",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_sync_outbox_binding_entity",
        "sync_outbox",
        ["binding_id", "entity_type", "entity_id"],
    )

    op.create_table(
        "sync_inbox",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("binding_id", UUID, nullable=False),
        sa.Column("sheet_name", sa.String(100), nullable=False),
        sa.Column("source_event_id", sa.String(255), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", UUID, nullable=True),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), server_default="received", nullable=False),
        sa.Column("validation_errors", JSONB, nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "entity_type IN ('account', 'category', 'transaction')",
            name="ck_sync_inbox_entity_type",
        ),
        sa.CheckConstraint(
            "status IN ('received', 'validated', 'applied', 'rejected', 'duplicate', "
            "'conflict', 'ignored')",
            name="ck_sync_inbox_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["binding_id"], ["google_sheet_bindings.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_sync_inbox"),
        sa.UniqueConstraint("idempotency_key", name="uq_sync_inbox_idempotency_key"),
        sa.UniqueConstraint(
            "binding_id", "source_event_id", name="uq_sync_inbox_binding_source_event"
        ),
    )
    op.create_index("ix_sync_inbox_binding_status", "sync_inbox", ["binding_id", "status"])

    op.create_table(
        "sync_conflicts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("binding_id", UUID, nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("database_version", sa.Integer(), nullable=False),
        sa.Column("sheet_version", sa.Integer(), nullable=True),
        sa.Column("database_payload", JSONB, nullable=False),
        sa.Column("sheet_payload", JSONB, nullable=False),
        sa.Column("conflicting_fields", JSONB, nullable=False),
        sa.Column("status", sa.String(30), server_default="open", nullable=False),
        sa.Column("resolution", sa.String(30), nullable=True),
        sa.Column("resolved_payload", JSONB, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", TIMESTAMP, nullable=True),
        sa.Column("resolved_by", UUID, nullable=True),
        sa.CheckConstraint(
            "entity_type IN ('account', 'category', 'transaction')",
            name="ck_sync_conflicts_entity_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'ignored', 'superseded')",
            name="ck_sync_conflicts_status",
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('keep_database', 'keep_sheet', 'manual_merge')",
            name="ck_sync_conflicts_resolution",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["binding_id"], ["google_sheet_bindings.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_sync_conflicts"),
    )
    op.create_index(
        "ix_sync_conflicts_workspace_status",
        "sync_conflicts",
        ["workspace_id", "status", "created_at"],
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("binding_id", UUID, nullable=False),
        sa.Column("run_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("started_at", TIMESTAMP, nullable=False),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column("processed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deleted_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("conflict_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("summary", JSONB, nullable=True),
        sa.Column("request_id", UUID, nullable=True),
        sa.Column("initiated_by", UUID, nullable=True),
        sa.CheckConstraint(
            "run_type IN ('initial_export', 'outbox_push', 'inbox_pull', 'manual_push', "
            "'manual_pull', 'reconciliation', 'template_upgrade')",
            name="ck_sync_runs_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed', 'cancelled')",
            name="ck_sync_runs_status",
        ),
        sa.CheckConstraint(
            "processed_count >= 0 AND created_count >= 0 AND updated_count >= 0 AND "
            "deleted_count >= 0 AND conflict_count >= 0 AND error_count >= 0",
            name="ck_sync_runs_counts",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["binding_id"], ["google_sheet_bindings.id"]),
        sa.ForeignKeyConstraint(["initiated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_sync_runs"),
    )
    op.create_index("ix_sync_runs_binding_started", "sync_runs", ["binding_id", "started_at"])

    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action", "audit_log", f"action IN ({STAGE4_AUDIT_ACTIONS})"
    )
    op.drop_constraint("ck_audit_log_source", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_source",
        "audit_log",
        "source IN ('manual', 'api', 'import', 'system', 'google_sheets', 'worker')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.execute(sa.text(f"DELETE FROM audit_log WHERE action IN ({STAGE4_ONLY_AUDIT_ACTIONS})"))
    op.drop_constraint("ck_audit_log_source", "audit_log", type_="check")
    op.execute(
        sa.text(
            "UPDATE audit_log SET source = 'system' WHERE source IN ('google_sheets', 'worker')"
        )
    )
    op.create_check_constraint(
        "ck_audit_log_source",
        "audit_log",
        "source IN ('manual', 'api', 'import', 'system')",
    )
    op.create_check_constraint(
        "ck_audit_log_action", "audit_log", f"action IN ({STAGE3_AUDIT_ACTIONS})"
    )
    op.drop_table("sync_runs")
    op.drop_table("sync_conflicts")
    op.drop_table("sync_inbox")
    op.drop_table("sync_outbox")
    op.drop_table("google_sheet_bindings")
    op.drop_table("google_oauth_flows")
    op.drop_table("google_connections")
    op.execute(sa.text("UPDATE transactions SET source = 'manual' WHERE source = 'google_sheets'"))
    op.drop_constraint("ck_transactions_source", "transactions", type_="check")
    op.create_check_constraint(
        "ck_transactions_source",
        "transactions",
        "source IN ('manual', 'api', 'import', 'system')",
    )
