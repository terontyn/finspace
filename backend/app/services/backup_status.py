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


async def get_backup_status(session: AsyncSession) -> BackupStatusResponse:
    created = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "backup.created")
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    verified = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "backup.verified")
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    if created is None:
        return BackupStatusResponse(
            status="missing",
            last_backup_at=None,
            last_verified_at=None,
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
    same_verified = verified is not None and (verified.after_data or {}).get(
        "sha256"
    ) == metadata.get("sha256")
    stale = age > settings.backup_stale_hours
    if not same_verified:
        status: Literal["healthy", "stale", "unverified"] = "unverified"
        warning = "Последняя резервная копия ещё не прошла проверку восстановления."
    elif stale:
        status = "stale"
        warning = f"Резервная копия старше {settings.backup_stale_hours} ч."
    else:
        status = "healthy"
        warning = None
    return BackupStatusResponse(
        status=status,
        last_backup_at=created.created_at,
        last_verified_at=verified.created_at if verified is not None else None,
        revision=revision,
        age_hours=age,
        sha256_short=f"{sha[:12]}..." if len(sha) == 64 else None,
        stale=stale or not same_verified,
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
