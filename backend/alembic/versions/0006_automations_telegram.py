"""Add service automations, recurring rules, Telegram integration and month close.

Revision ID: 0006_automations_telegram
Revises: 0005_apps_script_bridge
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_automations_telegram"
down_revision: str | None = "0005_apps_script_bridge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TIMESTAMP = sa.DateTime(timezone=True)

STAGE5_AUDIT_ACTIONS = (
    "'create', 'update', 'archive', 'delete', 'restore', 'cancel', 'reconcile', "
    "'user.register', 'user.login', 'user.logout', 'auth.session.revoked', "
    "'import.upload', 'import.mapping', 'import.validate', 'import.commit', "
    "'import.rollback', 'backup.created', 'backup.verified', 'restore.verified', "
    "'google.connect', 'google.disconnect', 'google.revoke', 'sheet.create', "
    "'sheet.initialize', 'sheet.pause', 'sheet.resume', 'sheet.full_export', "
    "'sheet.reconcile', 'sheet.webhook_secret.rotate', 'sync.push', 'sync.pull', "
    "'sync.conflict', 'sync.conflict.resolve', 'sync.error', 'template.upgrade', "
    "'sheet.bridge.create', 'sheet.bridge.register', 'sheet.bridge.secret.rotate', "
    "'sheet.bridge.delete', 'sync.ack', 'sync.heartbeat'"
)
AUTOMATION_AUDIT_ACTIONS = (
    "'service_account.create', 'service_account.key.rotate', 'service_account.revoke', "
    "'automation.run', 'recurring.create', 'recurring.update', 'recurring.execute', "
    "'recurring.pause', 'recurring.resume', 'telegram.link', 'telegram.unlink', "
    "'telegram.intent.create', 'telegram.intent.confirm', 'telegram.intent.cancel', "
    "'report.weekly.generate', 'report.uncategorized.generate', "
    "'month_close.prepare', 'month_close.confirm', 'month_close.reopen', "
    "'backup.remote.copy'"
)


def upgrade() -> None:
    op.create_table(
        "service_accounts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("service_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("permissions", JSONB, nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.Column("last_used_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "service_type IN ('n8n', 'backup_agent', 'integration')",
            name="ck_service_accounts_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'expired')",
            name="ck_service_accounts_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_service_accounts"),
    )
    op.create_index(
        "ix_service_accounts_workspace_status",
        "service_accounts",
        ["workspace_id", "status"],
    )

    op.create_table(
        "service_api_keys",
        sa.Column("id", UUID, nullable=False),
        sa.Column("service_account_id", UUID, nullable=False),
        sa.Column("key_prefix", sa.String(20), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=True),
        sa.Column("last_used_at", TIMESTAMP, nullable=True),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint("length(key_hash) = 64", name="ck_service_api_keys_hash"),
        sa.ForeignKeyConstraint(["service_account_id"], ["service_accounts.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_service_api_keys"),
        sa.UniqueConstraint("key_prefix", name="uq_service_api_keys_prefix"),
    )
    op.create_index(
        "ix_service_api_keys_account_active",
        "service_api_keys",
        ["service_account_id", "revoked_at", "expires_at"],
    )

    op.create_table(
        "automation_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=True),
        sa.Column("automation_type", sa.String(100), nullable=False),
        sa.Column("trigger_type", sa.String(50), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("service_account_id", UUID, nullable=True),
        sa.Column("initiated_by", UUID, nullable=True),
        sa.Column("started_at", TIMESTAMP, nullable=False),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column("input_summary", JSONB, nullable=True),
        sa.Column("result_summary", JSONB, nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("request_id", UUID, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'skipped', 'waiting_confirmation', "
            "'failed', 'cancelled')",
            name="ck_automation_runs_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["service_account_id"], ["service_accounts.id"]),
        sa.ForeignKeyConstraint(["initiated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_automation_runs"),
        sa.UniqueConstraint("idempotency_key", name="uq_automation_runs_idempotency"),
    )
    op.create_index(
        "ix_automation_runs_workspace_started",
        "automation_runs",
        ["workspace_id", "started_at"],
    )
    op.create_index(
        "ix_automation_runs_status_started", "automation_runs", ["status", "started_at"]
    )

    op.create_table(
        "recurring_rules",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("rule_type", sa.String(30), nullable=False),
        sa.Column("schedule_rrule", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("transaction_type", sa.String(30), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("target_account_id", UUID, nullable=True),
        sa.Column("category_id", UUID, nullable=True),
        sa.Column("counterparty", sa.String(300), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("creation_mode", sa.String(30), server_default="draft", nullable=False),
        sa.Column("days_before_reminder", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("next_run_at", TIMESTAMP, nullable=True),
        sa.Column("last_run_at", TIMESTAMP, nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "rule_type IN ('income', 'expense', 'transfer')",
            name="ck_recurring_rules_type",
        ),
        sa.CheckConstraint(
            "transaction_type IN ('income', 'expense', 'transfer')",
            name="ck_recurring_rules_transaction_type",
        ),
        sa.CheckConstraint(
            "creation_mode IN ('draft', 'confirmed', 'reminder_only')",
            name="ck_recurring_rules_creation_mode",
        ),
        sa.CheckConstraint("amount > 0", name="ck_recurring_rules_amount"),
        sa.CheckConstraint("days_before_reminder >= 0", name="ck_recurring_rules_reminder_days"),
        sa.CheckConstraint("version > 0", name="ck_recurring_rules_version"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["target_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_recurring_rules"),
    )
    op.create_index(
        "ix_recurring_rules_due",
        "recurring_rules",
        ["is_active", "next_run_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_recurring_rules_workspace", "recurring_rules", ["workspace_id", "name"])

    op.create_table(
        "recurring_rule_executions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("rule_id", UUID, nullable=False),
        sa.Column("scheduled_for", TIMESTAMP, nullable=False),
        sa.Column("automation_run_id", UUID, nullable=False),
        sa.Column("transaction_id", UUID, nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "status IN ('created', 'draft_created', 'confirmed_created', 'reminder_sent', "
            "'skipped', 'failed')",
            name="ck_recurring_rule_executions_status",
        ),
        sa.ForeignKeyConstraint(["rule_id"], ["recurring_rules.id"]),
        sa.ForeignKeyConstraint(["automation_run_id"], ["automation_runs.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_recurring_rule_executions"),
        sa.UniqueConstraint("rule_id", "scheduled_for", name="uq_recurring_rule_scheduled"),
    )
    op.create_index(
        "ix_recurring_rule_executions_rule_created",
        "recurring_rule_executions",
        ["rule_id", "created_at"],
    )

    op.create_table(
        "telegram_links",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(100), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("linked_at", TIMESTAMP, nullable=False),
        sa.Column("last_seen_at", TIMESTAMP, nullable=True),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'blocked')",
            name="ck_telegram_links_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_links"),
        sa.UniqueConstraint("telegram_user_id", name="uq_telegram_links_user_id"),
    )
    op.create_index(
        "ix_telegram_links_workspace_user_status",
        "telegram_links",
        ["workspace_id", "user_id", "status"],
    )

    op.create_table(
        "telegram_link_codes",
        sa.Column("id", UUID, nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("code_prefix", sa.String(4), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("used_at", TIMESTAMP, nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_telegram_link_codes_attempts"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_link_codes"),
        sa.UniqueConstraint("code_hash", name="uq_telegram_link_codes_hash"),
    )
    op.create_index(
        "ix_telegram_link_codes_lookup",
        "telegram_link_codes",
        ["code_prefix", "expires_at", "used_at"],
    )

    op.create_table(
        "telegram_intents",
        sa.Column("id", UUID, nullable=False),
        sa.Column("opaque_id", sa.String(32), nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("intent_type", sa.String(50), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled', 'expired', 'failed')",
            name="ck_telegram_intents_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_intents"),
        sa.UniqueConstraint("opaque_id", name="uq_telegram_intents_opaque_id"),
    )
    op.create_index(
        "ix_telegram_intents_workspace_status",
        "telegram_intents",
        ["workspace_id", "status", "expires_at"],
    )

    op.create_table(
        "month_closures",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("prepared_by", UUID, nullable=True),
        sa.Column("confirmed_by", UUID, nullable=True),
        sa.Column("prepared_at", TIMESTAMP, nullable=True),
        sa.Column("confirmed_at", TIMESTAMP, nullable=True),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column("blocking_issues", JSONB, nullable=True),
        sa.Column("warning_issues", JSONB, nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'blocked', 'confirmed', 'reopened')",
            name="ck_month_closures_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_month_closures_version"),
        sa.CheckConstraint(
            "date_trunc('month', period_month)::date = period_month",
            name="ck_month_closures_period_month",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["prepared_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_month_closures"),
        sa.UniqueConstraint("workspace_id", "period_month", name="uq_month_closures_period"),
    )
    op.create_index(
        "ix_month_closures_workspace_status", "month_closures", ["workspace_id", "status"]
    )

    op.create_table(
        "notification_settings",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("schedule_time", sa.Time(), nullable=True),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("configuration", JSONB, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "channel IN ('telegram', 'in_app')",
            name="ck_notification_settings_channel",
        ),
        sa.CheckConstraint(
            "event_type IN ('weekly_report', 'uncategorized_reminder', 'recurring_due', "
            "'recurring_created', 'month_close', 'backup_problem', 'sync_problem')",
            name="ck_notification_settings_event_type",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_notification_settings"),
        sa.UniqueConstraint(
            "workspace_id", "user_id", "channel", "event_type", name="uq_notification_settings"
        ),
    )
    op.create_index(
        "ix_notification_settings_delivery",
        "notification_settings",
        ["channel", "event_type", "enabled"],
    )

    op.drop_constraint("ck_transactions_source", "transactions", type_="check")
    op.create_check_constraint(
        "ck_transactions_source",
        "transactions",
        "source IN ('manual', 'api', 'import', 'system', 'google_sheets', "
        "'automation', 'telegram')",
    )
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_action",
        "audit_log",
        f"action IN ({STAGE5_AUDIT_ACTIONS}, {AUTOMATION_AUDIT_ACTIONS})",
    )
    op.drop_constraint("ck_audit_log_source", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_source",
        "audit_log",
        "source IN ('manual', 'api', 'import', 'system', 'google_sheets', 'worker', "
        "'automation', 'telegram')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_audit_log_action", "audit_log", type_="check")
    op.execute(sa.text(f"DELETE FROM audit_log WHERE action IN ({AUTOMATION_AUDIT_ACTIONS})"))
    op.create_check_constraint(
        "ck_audit_log_action", "audit_log", f"action IN ({STAGE5_AUDIT_ACTIONS})"
    )
    op.drop_constraint("ck_audit_log_source", "audit_log", type_="check")
    op.execute(
        sa.text("UPDATE audit_log SET source = 'system' WHERE source IN ('automation', 'telegram')")
    )
    op.create_check_constraint(
        "ck_audit_log_source",
        "audit_log",
        "source IN ('manual', 'api', 'import', 'system', 'google_sheets', 'worker')",
    )
    op.drop_constraint("ck_transactions_source", "transactions", type_="check")
    op.execute(
        sa.text(
            "UPDATE transactions SET source = 'system' WHERE source IN ('automation', 'telegram')"
        )
    )
    op.create_check_constraint(
        "ck_transactions_source",
        "transactions",
        "source IN ('manual', 'api', 'import', 'system', 'google_sheets')",
    )
    op.drop_table("notification_settings")
    op.drop_table("month_closures")
    op.drop_table("telegram_intents")
    op.drop_table("telegram_link_codes")
    op.drop_table("telegram_links")
    op.drop_table("recurring_rule_executions")
    op.drop_table("recurring_rules")
    op.drop_table("automation_runs")
    op.drop_table("service_api_keys")
    op.drop_table("service_accounts")
