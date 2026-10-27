"""Add the Apps Script Bridge provider.

Revision ID: 0005_apps_script_bridge
Revises: 0004_google_sheets_sync
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_apps_script_bridge"
down_revision: str | None = "0004_google_sheets_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMP = sa.DateTime(timezone=True)

STAGE4_AUDIT_ACTIONS = (
    "'create', 'update', 'archive', 'delete', 'restore', 'cancel', 'reconcile', "
    "'user.register', 'user.login', 'user.logout', 'auth.session.revoked', "
    "'import.upload', 'import.mapping', 'import.validate', 'import.commit', "
    "'import.rollback', 'backup.created', 'backup.verified', 'restore.verified', "
    "'google.connect', 'google.disconnect', 'google.revoke', 'sheet.create', "
    "'sheet.initialize', 'sheet.pause', 'sheet.resume', 'sheet.full_export', "
    "'sheet.reconcile', 'sheet.webhook_secret.rotate', 'sync.push', 'sync.pull', "
    "'sync.conflict', 'sync.conflict.resolve', 'sync.error', 'template.upgrade'"
)
BRIDGE_AUDIT_ACTIONS = (
    "'sheet.bridge.create', 'sheet.bridge.register', 'sheet.bridge.secret.rotate', "
    "'sheet.bridge.delete', 'sync.ack', 'sync.heartbeat'"
)


def upgrade() -> None:
    op.add_column(
        "google_sheet_bindings",
        sa.Column(
            "provider",
            sa.String(40),
            server_default="apps_script_bridge",
            nullable=False,
        ),
    )
    op.add_column(
        "google_sheet_bindings",
        sa.Column("binding_secret_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "google_sheet_bindings",
        sa.Column("binding_secret_created_at", TIMESTAMP, nullable=True),
    )
    op.add_column(
        "google_sheet_bindings",
        sa.Column("binding_secret_last_rotated_at", TIMESTAMP, nullable=True),
    )
    op.add_column(
        "google_sheet_bindings",
        sa.Column("last_heartbeat_at", TIMESTAMP, nullable=True),
    )
    op.add_column(
        "google_sheet_bindings",
        sa.Column("last_ack_at", TIMESTAMP, nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE google_sheet_bindings SET "
            "provider = 'google_oauth', "
            "binding_secret_hash = COALESCE(webhook_secret_hash, repeat('0', 64)), "
            "binding_secret_created_at = created_at, "
            "binding_secret_last_rotated_at = webhook_secret_rotated_at"
        )
    )
    op.alter_column("google_sheet_bindings", "binding_secret_hash", nullable=False)
    op.alter_column("google_sheet_bindings", "binding_secret_created_at", nullable=False)
    op.alter_column("google_sheet_bindings", "google_connection_id", nullable=True)
    op.alter_column("google_sheet_bindings", "spreadsheet_id", nullable=True)
    op.alter_column("google_sheet_bindings", "spreadsheet_url", nullable=True)
    op.create_check_constraint(
        "ck_google_sheet_bindings_provider",
        "google_sheet_bindings",
        "provider IN ('apps_script_bridge', 'google_oauth')",
    )
    op.create_index(
        "ix_google_sheet_bindings_provider_status",
        "google_sheet_bindings",
        ["provider", "status"],
    )
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action",
        "audit_log",
        f"action IN ({STAGE4_AUDIT_ACTIONS}, {BRIDGE_AUDIT_ACTIONS})",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM google_sheet_bindings "
            "WHERE provider = 'apps_script_bridge') THEN "
            "RAISE EXCEPTION 'Remove Apps Script Bridge bindings before downgrade'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.execute(sa.text(f"DELETE FROM audit_log WHERE action IN ({BRIDGE_AUDIT_ACTIONS})"))
    op.create_check_constraint(
        "ck_audit_log_action",
        "audit_log",
        f"action IN ({STAGE4_AUDIT_ACTIONS})",
    )
    op.drop_index(
        "ix_google_sheet_bindings_provider_status",
        table_name="google_sheet_bindings",
    )
    op.drop_constraint(
        "ck_google_sheet_bindings_provider",
        "google_sheet_bindings",
        type_="check",
    )
    op.alter_column("google_sheet_bindings", "spreadsheet_url", nullable=False)
    op.alter_column("google_sheet_bindings", "spreadsheet_id", nullable=False)
    op.alter_column("google_sheet_bindings", "google_connection_id", nullable=False)
    op.drop_column("google_sheet_bindings", "last_ack_at")
    op.drop_column("google_sheet_bindings", "last_heartbeat_at")
    op.drop_column("google_sheet_bindings", "binding_secret_last_rotated_at")
    op.drop_column("google_sheet_bindings", "binding_secret_created_at")
    op.drop_column("google_sheet_bindings", "binding_secret_hash")
    op.drop_column("google_sheet_bindings", "provider")
