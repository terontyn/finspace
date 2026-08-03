from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.services.backup_status import (
    DisabledBackupRemoteProvider,
    LocalSecondaryPathProvider,
)


def test_local_secondary_provider_copies_only_dump_and_manifest(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    filename = "finspace_2026-08-01T000000Z.dump"
    (primary / filename).write_bytes(b"verified-dump")
    (primary / f"{filename}.manifest.json").write_text('{"sha256":"test"}', encoding="utf-8")
    (primary / "unrelated.secret").write_text("must-not-copy", encoding="utf-8")

    copied = LocalSecondaryPathProvider(secondary).copy_verified(primary, filename)

    assert copied == [filename, f"{filename}.manifest.json"]
    assert (secondary / filename).read_bytes() == b"verified-dump"
    assert not (secondary / "unrelated.secret").exists()


def test_secondary_provider_rejects_primary_directory(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    with pytest.raises(ApiError):
        LocalSecondaryPathProvider(primary).copy_verified(primary, "backup.dump")


def test_disabled_provider_never_copies(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as error:
        DisabledBackupRemoteProvider().copy_verified(tmp_path, "backup.dump")
    assert error.value.code == "BACKUP_REMOTE_DISABLED"
