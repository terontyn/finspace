import uuid

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import SoftDeleteMixin, TimestampMixin, VersionMixin


class Payee(Base, TimestampMixin, VersionMixin, SoftDeleteMixin):
    __tablename__ = "payees"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_payees_id_workspace"),
        CheckConstraint("version >= 1", name="ck_payees_version"),
        Index("ix_payees_workspace_lifecycle_name", "workspace_id", "deleted_at", "name", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    aliases: Mapped[list["PayeeAlias"]] = relationship(
        back_populates="payee",
        lazy="raise",
        order_by="PayeeAlias.created_at, PayeeAlias.id",
    )


class PayeeAlias(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "payee_aliases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["payee_id", "workspace_id"],
            ["payees.id", "payees.workspace_id"],
            name="fk_payee_aliases_payee_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "normalized_alias_hash",
            name="uq_payee_aliases_workspace_hash",
        ),
        CheckConstraint(
            "length(normalized_alias) > 0", name="ck_payee_aliases_normalized_not_empty"
        ),
        CheckConstraint(
            "normalized_alias_hash ~ '^[0-9a-f]{64}$'",
            name="ck_payee_aliases_hash_format",
        ),
        CheckConstraint(
            "NOT is_primary OR deleted_at IS NULL",
            name="ck_payee_aliases_primary_not_deleted",
        ),
        Index(
            "ix_payee_aliases_payee_lifecycle",
            "workspace_id",
            "payee_id",
            "deleted_at",
            "is_primary",
            "id",
        ),
        Index(
            "uq_payee_aliases_active_primary",
            "payee_id",
            unique=True,
            postgresql_where=text("is_primary = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    alias: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    payee: Mapped[Payee] = relationship(back_populates="aliases", lazy="raise")
