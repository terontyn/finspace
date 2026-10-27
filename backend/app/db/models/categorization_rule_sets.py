import uuid

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import TimestampMixin, VersionMixin


class CategorizationRuleSetControl(Base, TimestampMixin, VersionMixin):
    """Workspace-scoped concurrency authority for the deterministic categorization rule set.

    ``version`` is a monotonically increasing revision of the rule set as a whole. It is bumped by
    every rule mutation that can change which rule matches first, and it is the row every rule
    mutation locks ``FOR UPDATE`` before touching a rule. Single-transaction categorization holds
    the same row ``FOR SHARE`` while it proves and commits its match, so concurrent applies stay
    compatible while rule mutations wait.
    """

    __tablename__ = "categorization_rule_set_controls"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_categorization_rule_set_controls_version"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
