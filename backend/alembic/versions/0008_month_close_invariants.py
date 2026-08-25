"""Add hard month-close controls and immutable revisions.

Revision ID: 0008_month_close_invariants
Revises: 0007_account_reconciliation
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_month_close_invariants"
down_revision: str | None = "0007_account_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMP = sa.DateTime(timezone=True)


def _next_month(value: date) -> date:
    return date(value.year + 1, 1, 1) if value.month == 12 else date(value.year, value.month + 1, 1)


def _period_bounds(period: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(period, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(_next_month(period), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


def _legacy_closed_through(rows: Sequence[Mapping[str, object]]) -> date | None:
    """Derive a cutoff only when the complete legacy history proves one active chain."""
    confirmed_periods = [row["period_month"] for row in rows if row["status"] == "confirmed"]
    if not confirmed_periods:
        return None
    periods = [item for item in confirmed_periods if isinstance(item, date)]
    if len(periods) != len(confirmed_periods):
        return None
    latest = periods[-1]
    if any(
        row["status"] != "confirmed"
        and isinstance(row["period_month"], date)
        and row["period_month"] <= latest
        for row in rows
    ):
        return None
    if not all(current == _next_month(previous) for previous, current in pairwise(periods)):
        return None
    return _next_month(latest) - timedelta(days=1)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_month_closures_id_workspace",
        "month_closures",
        ["id", "workspace_id"],
    )
    op.create_table(
        "month_close_controls",
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("closed_through", sa.Date(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("backup_policy", sa.String(30), server_default="warn", nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_month_close_controls_version"),
        sa.CheckConstraint(
            "backup_policy IN ('warn', 'require_healthy')",
            name="ck_month_close_controls_backup_policy",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", name="pk_month_close_controls"),
    )
    op.create_table(
        "month_close_revisions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("closure_id", UUID, nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("period_start_at", TIMESTAMP, nullable=False),
        sa.Column("period_end_at", TIMESTAMP, nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("financial_fingerprint", sa.String(64), nullable=True),
        sa.Column("legacy_unverified", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("confirmed_by", UUID, nullable=False),
        sa.Column("confirmed_at", TIMESTAMP, nullable=False),
        sa.Column("request_id", UUID, nullable=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("revision_number > 0", name="ck_month_close_revisions_revision_number"),
        sa.CheckConstraint(
            "financial_fingerprint IS NULL OR financial_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_month_close_revisions_fingerprint",
        ),
        sa.CheckConstraint(
            "legacy_unverified OR financial_fingerprint IS NOT NULL",
            name="ck_month_close_revisions_fingerprint_required",
        ),
        sa.CheckConstraint(
            "date_trunc('month', period_month)::date = period_month",
            name="ck_month_close_revisions_period_month",
        ),
        sa.CheckConstraint(
            "period_end_at > period_start_at",
            name="ck_month_close_revisions_period_bounds",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["closure_id", "workspace_id"],
            ["month_closures.id", "month_closures.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_month_close_revisions"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_month_close_revisions_id_workspace"),
        sa.UniqueConstraint(
            "closure_id",
            "revision_number",
            name="uq_month_close_revisions_number",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_month_close_revisions_workspace_idempotency",
        ),
    )
    op.create_index(
        "ix_month_close_revisions_workspace_period_revision",
        "month_close_revisions",
        ["workspace_id", "period_month", "revision_number"],
    )

    op.add_column("month_closures", sa.Column("prepare_token", sa.String(64), nullable=True))
    op.add_column("month_closures", sa.Column("prepared_fingerprint", sa.String(64), nullable=True))
    op.add_column("month_closures", sa.Column("current_revision_id", UUID, nullable=True))
    op.add_column("month_closures", sa.Column("last_reopened_at", TIMESTAMP, nullable=True))
    op.add_column("month_closures", sa.Column("last_reopened_by", UUID, nullable=True))
    op.add_column("month_closures", sa.Column("last_reopen_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_month_closures_prepare_token",
        "month_closures",
        "prepare_token IS NULL OR prepare_token ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_month_closures_prepared_fingerprint",
        "month_closures",
        "prepared_fingerprint IS NULL OR prepared_fingerprint ~ '^[0-9a-f]{64}$'",
    )
    op.create_foreign_key(
        "fk_month_closures_last_reopened_by_users",
        "month_closures",
        "users",
        ["last_reopened_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_month_closures_current_revision_workspace",
        "month_closures",
        "month_close_revisions",
        ["current_revision_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )

    _backfill()


def _backfill() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    workspaces = sa.Table("workspaces", metadata, autoload_with=bind)
    closures = sa.Table("month_closures", metadata, autoload_with=bind)
    controls = sa.Table("month_close_controls", metadata, autoload_with=bind)
    revisions = sa.Table("month_close_revisions", metadata, autoload_with=bind)

    workspace_rows = bind.execute(sa.select(workspaces.c.id, workspaces.c.timezone)).mappings()
    now = datetime.now(UTC)
    for workspace in workspace_rows:
        workspace_id = workspace["id"]
        rows = list(
            bind.execute(
                sa.select(closures)
                .where(closures.c.workspace_id == workspace_id)
                .order_by(closures.c.period_month)
            ).mappings()
        )
        confirmed = [row for row in rows if row["status"] == "confirmed"]
        for row in confirmed:
            if row["confirmed_by"] is None or row["confirmed_at"] is None:
                raise RuntimeError(
                    f"Legacy confirmed month closure is missing confirmation evidence: {row['id']}"
                )
            start, end = _period_bounds(row["period_month"], workspace["timezone"])
            revision_id = uuid.uuid4()
            bind.execute(
                revisions.insert().values(
                    id=revision_id,
                    workspace_id=workspace_id,
                    closure_id=row["id"],
                    revision_number=1,
                    period_month=row["period_month"],
                    period_start_at=start,
                    period_end_at=end,
                    snapshot={
                        **dict(row["summary"] or {}),
                        "legacy_unverified": True,
                    },
                    financial_fingerprint=None,
                    legacy_unverified=True,
                    confirmed_by=row["confirmed_by"],
                    confirmed_at=row["confirmed_at"],
                    request_id=None,
                    source="migration",
                    idempotency_key=f"legacy:{row['id']}:1",
                    created_at=now,
                )
            )
            bind.execute(
                closures.update()
                .where(closures.c.id == row["id"])
                .values(current_revision_id=revision_id)
            )

        closed_through = _legacy_closed_through(rows)
        bind.execute(
            controls.insert().values(
                workspace_id=workspace_id,
                closed_through=closed_through,
                version=1,
                backup_policy="warn",
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    op.drop_constraint(
        "fk_month_closures_current_revision_workspace",
        "month_closures",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_month_closures_last_reopened_by_users", "month_closures", type_="foreignkey"
    )
    op.drop_constraint("ck_month_closures_prepared_fingerprint", "month_closures", type_="check")
    op.drop_constraint("ck_month_closures_prepare_token", "month_closures", type_="check")
    op.drop_column("month_closures", "last_reopen_reason")
    op.drop_column("month_closures", "last_reopened_by")
    op.drop_column("month_closures", "last_reopened_at")
    op.drop_column("month_closures", "current_revision_id")
    op.drop_column("month_closures", "prepared_fingerprint")
    op.drop_column("month_closures", "prepare_token")
    op.drop_table("month_close_revisions")
    op.drop_table("month_close_controls")
    op.drop_constraint("uq_month_closures_id_workspace", "month_closures", type_="unique")
