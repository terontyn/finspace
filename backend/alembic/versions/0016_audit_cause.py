"""Add generic mutation-cause columns to the audit log.

Revision ID: 0016_audit_cause
Revises: 0015_categorization_apply_ops

Stage B needs a durable answer to "what caused this transaction mutation". The audit log is a
single flat table shared by every entity, so the cause is modelled generically
(``cause_type`` / ``cause_id`` / ``cause_metadata``) rather than as a one-off
``categorization_rule_id`` column: a second cause would otherwise require another schema rewrite.

Deliberately no foreign key to ``categorization_rules``. Historical audit must stay readable after
a rule is archived or hard-deleted, and a restrictive FK would either block that lifecycle or
cascade away the evidence. ``cause_id`` is immutable UUID evidence, not a live reference.

All three columns are nullable: existing rows stay valid untouched, and ordinary mutations that
have no cause keep writing exactly what they wrote before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_audit_cause"
down_revision: str | None = "0015_categorization_apply_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("cause_type", sa.String(length=50), nullable=True))
    op.add_column("audit_log", sa.Column("cause_id", UUID, nullable=True))
    op.add_column(
        "audit_log",
        sa.Column("cause_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # Answers "which mutations did this rule cause" without scanning the whole log. Partial, so
    # the index stays small: the overwhelming majority of audit rows carry no cause.
    op.create_index(
        "ix_audit_log_cause",
        "audit_log",
        ["cause_type", "cause_id"],
        unique=False,
        postgresql_where=sa.text("cause_type IS NOT NULL"),
    )
    # A cause is either fully present or fully absent; a dangling type without an id would be
    # unreadable evidence.
    op.create_check_constraint(
        "cause_complete",
        "audit_log",
        "(cause_type IS NULL AND cause_id IS NULL)"
        " OR (cause_type IS NOT NULL AND cause_id IS NOT NULL)",
    )


def downgrade() -> None:
    # The metadata naming convention expands "cause_complete" to
    # "ck_audit_log_cause_complete" on both create and drop, so pass the bare name here too.
    op.drop_constraint("cause_complete", "audit_log", type_="check")
    op.drop_index("ix_audit_log_cause", table_name="audit_log")
    op.drop_column("audit_log", "cause_metadata")
    op.drop_column("audit_log", "cause_id")
    op.drop_column("audit_log", "cause_type")
