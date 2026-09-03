import json
import shutil
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.audit import AuditLog
from app.schemas.automations import BackupStatusResponse

# Evidence rows are written once per backup run, so a short newest-first window always contains the
# rows for the current backup while keeping the query bounded on a growing audit log.
_EVIDENCE_SCAN_LIMIT = 20
_LABEL_MAX_LENGTH = 60


async def _matching_evidence(
    session: AsyncSession,
    action: str,
    filename: str,
    sha256: str,
) -> AuditLog | None:
    """Return the newest evidence row describing *this* backup, not merely the newest row.

    Correlating on both filename and digest is what stops an older off-host copy from vouching for
    a newer dump. The query stays bounded: one action scan, newest first, small limit.
    """
    if not filename or not sha256:
        return None
    rows = await session.scalars(
        select(AuditLog)
        .where(AuditLog.action == action)
        .order_by(AuditLog.created_at.desc())
        .limit(_EVIDENCE_SCAN_LIMIT)
    )
    for row in rows:
        payload = row.after_data or {}
        if payload.get("sha256") != sha256:
            continue
        recorded_name = payload.get("filename")
        # backup.verified predates the filename field, so a digest match alone still counts there.
        if recorded_name is not None and recorded_name != filename:
            continue
        return row
    return None


async def get_backup_status(session: AsyncSession) -> BackupStatusResponse:
    created = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "backup.created")
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    if created is None:
        return BackupStatusResponse(
            status="missing",
            last_backup_at=None,
            last_verified_at=None,
            last_offhost_at=None,
            offhost_destination_label=None,
            revision=None,
            age_hours=None,
            sha256_short=None,
            stale=True,
            warning="Проверенная резервная копия не найдена.",
        )
    now = datetime.now(UTC)
    age = Decimal(str((now - created.created_at).total_seconds() / 3600)).quantize(Decimal("0.01"))
    metadata = created.after_data or {}
    filename = str(metadata.get("filename", ""))
    sha = str(metadata.get("sha256", ""))
    revision = _manifest_revision(filename)

    verified = await _matching_evidence(session, "backup.verified", filename, sha)
    offhost = await _matching_evidence(session, "backup.remote.copy", filename, sha)
    stale = age > settings.backup_stale_hours

    # Precedence is deliberate: local verification, then off-host confirmation, then age. A backup
    # that exists only on this machine is not a disaster-recovery backup, so it is never healthy.
    if verified is None:
        status: Literal["healthy", "stale", "unverified"] = "unverified"
        warning = "Последняя резервная копия ещё не прошла проверку восстановления."
    elif offhost is None:
        status = "unverified"
        warning = "Последняя проверенная копия ещё не подтверждена вне этого хоста."
    elif stale:
        status = "stale"
        warning = f"Резервная копия старше {settings.backup_stale_hours} ч."
    else:
        status = "healthy"
        warning = None

    label = None
    if offhost is not None:
        raw_label = (offhost.after_data or {}).get("destination_label")
        if isinstance(raw_label, str) and raw_label:
            # An opaque operator label, never a security boundary: truncated, never a host or path.
            label = raw_label[:_LABEL_MAX_LENGTH]

    return BackupStatusResponse(
        status=status,
        last_backup_at=created.created_at,
        last_verified_at=verified.created_at if verified is not None else None,
        last_offhost_at=offhost.created_at if offhost is not None else None,
        offhost_destination_label=label,
        revision=revision,
        age_hours=age,
        sha256_short=f"{sha[:12]}..." if len(sha) == 64 else None,
        stale=status != "healthy",
        warning=warning,
    )


def _manifest_revision(filename: str) -> str | None:
    if not filename or Path(filename).name != filename:
        return None
    manifest = settings.backup_metadata_path / f"{filename}.manifest.json"
    try:
        resolved_root = settings.backup_metadata_path.resolve()
        resolved_manifest = manifest.resolve()
        if resolved_root not in resolved_manifest.parents:
            return None
        payload = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    revision = payload.get("alembic_revision")
    return str(revision)[:100] if revision else None


class BackupRemoteProvider(ABC):
    @abstractmethod
    def copy_verified(self, backup_root: Path, filename: str) -> list[str]:
        """Copy one verified dump and its manifest; return copied basenames."""


class DisabledBackupRemoteProvider(BackupRemoteProvider):
    def copy_verified(self, backup_root: Path, filename: str) -> list[str]:
        raise ApiError(
            status_code=409,
            code="BACKUP_REMOTE_DISABLED",
            message="Secondary backup provider is disabled",
        )


class LocalSecondaryPathProvider(BackupRemoteProvider):
    def __init__(self, target: Path) -> None:
        self.target = target

    def copy_verified(self, backup_root: Path, filename: str) -> list[str]:
        if Path(filename).name != filename or not filename.endswith(".dump"):
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="Backup filename is invalid",
            )
        source_root = backup_root.resolve(strict=True)
        target_root = self.target.resolve(strict=True)
        if source_root == target_root:
            raise ApiError(
                status_code=409,
                code="VALIDATION_ERROR",
                message="Secondary backup target must differ from the primary directory",
            )
        sources = [source_root / filename, source_root / f"{filename}.manifest.json"]
        if any(not item.is_file() for item in sources):
            raise ApiError(
                status_code=404,
                code="BACKUP_NOT_FOUND",
                message="Verified backup files were not found",
            )
        copied: list[str] = []
        for source in sources:
            destination = target_root / source.name
            shutil.copy2(source, destination)
            copied.append(destination.name)
        return copied


def remote_provider() -> BackupRemoteProvider:
    if settings.backup_remote_provider == "disabled":
        return DisabledBackupRemoteProvider()
    if settings.backup_secondary_path is None:
        raise ApiError(
            status_code=409,
            code="VALIDATION_ERROR",
            message="BACKUP_SECONDARY_PATH is required",
        )
    return LocalSecondaryPathProvider(settings.backup_secondary_path)
