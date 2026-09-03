"""Backup health is evidence-driven, and the evidence must describe *this* backup.

The three audit actions a backup run writes — ``backup.created``, ``backup.verified`` and
``backup.remote.copy`` — are correlated by filename and digest, so an older off-host copy can never
vouch for a newer dump. A backup that exists only on this host is deliberately never ``healthy``:
local-only storage is not disaster recovery.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.session import AsyncSessionFactory
from app.schemas.automations import BackupStatusResponse
from app.services.backup_status import get_backup_status

DUMP = "finspace_2026-09-03T010000Z.dump"
SHA = "a" * 64
OTHER_SHA = "b" * 64


async def _clear() -> None:
    async with AsyncSessionFactory() as session:
        await session.execute(
            delete(AuditLog).where(
                AuditLog.action.in_(("backup.created", "backup.verified", "backup.remote.copy"))
            )
        )
        await session.commit()


async def _record(action: str, payload: dict, *, age_hours: float = 0.0) -> None:
    async with AsyncSessionFactory() as session:
        session.add(
            AuditLog(
                workspace_id=None,
                actor_user_id=None,
                entity_type="backup",
                entity_id=uuid.uuid4(),
                action=action,
                before_data=None,
                after_data=payload,
                request_id=None,
                source="system",
                created_at=datetime.now(UTC) - timedelta(hours=age_hours),
            )
        )
        await session.commit()


async def _status() -> BackupStatusResponse:
    async with AsyncSessionFactory() as session:
        return await get_backup_status(session)


async def _created(*, age_hours: float = 0.0, sha: str = SHA, filename: str = DUMP) -> None:
    await _record(
        "backup.created",
        {"filename": filename, "sha256": sha, "size_bytes": 1024},
        age_hours=age_hours,
    )


@pytest.fixture(autouse=True)
async def _clean_evidence():
    await _clear()
    yield
    await _clear()


async def test_no_backup_at_all_is_missing() -> None:
    result = await _status()

    assert result.status == "missing"
    assert result.stale is True
    assert result.last_backup_at is None
    assert result.last_verified_at is None
    assert result.last_offhost_at is None
    assert result.offhost_destination_label is None


async def test_created_without_verification_is_unverified() -> None:
    await _created()

    result = await _status()

    assert result.status == "unverified"
    assert result.stale is True
    assert result.last_verified_at is None
    assert result.warning is not None
    assert "восстановлен" in result.warning


async def test_verification_of_a_different_backup_does_not_count() -> None:
    await _created()
    await _record("backup.verified", {"filename": "finspace_older.dump", "sha256": OTHER_SHA})

    result = await _status()

    assert result.status == "unverified"
    assert result.last_verified_at is None


async def test_local_verification_without_offhost_is_not_healthy() -> None:
    await _created()
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA})

    result = await _status()

    # The decisive Stage B rule: a verified local copy is still not a disaster-recovery backup.
    assert result.status == "unverified"
    assert result.stale is True
    assert result.last_verified_at is not None
    assert result.last_offhost_at is None
    assert result.warning is not None
    assert "вне этого хоста" in result.warning


async def test_offhost_copy_of_another_filename_does_not_count() -> None:
    await _created()
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA})
    await _record(
        "backup.remote.copy",
        {"filename": "finspace_2026-09-02T010000Z.dump", "sha256": SHA, "destination_label": "nas"},
    )

    result = await _status()

    assert result.status == "unverified"
    assert result.last_offhost_at is None
    assert result.offhost_destination_label is None


async def test_offhost_copy_with_another_digest_does_not_count() -> None:
    await _created()
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA})
    await _record(
        "backup.remote.copy",
        {"filename": DUMP, "sha256": OTHER_SHA, "destination_label": "nas"},
    )

    result = await _status()

    assert result.status == "unverified"
    assert result.last_offhost_at is None


async def test_yesterdays_offhost_copy_cannot_vouch_for_todays_backup() -> None:
    # A complete, healthy run yesterday, then a fresh dump today that has not left the host.
    old_dump = "finspace_2026-09-02T010000Z.dump"
    await _created(filename=old_dump, sha=OTHER_SHA, age_hours=24)
    await _record("backup.verified", {"filename": old_dump, "sha256": OTHER_SHA}, age_hours=24)
    await _record(
        "backup.remote.copy",
        {"filename": old_dump, "sha256": OTHER_SHA, "destination_label": "nas"},
        age_hours=24,
    )
    await _created()
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA})

    result = await _status()

    assert result.status == "unverified"
    assert result.last_offhost_at is None
    assert result.offhost_destination_label is None


async def test_verified_and_confirmed_offhost_is_healthy() -> None:
    await _created()
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA})
    await _record(
        "backup.remote.copy",
        {"filename": DUMP, "sha256": SHA, "destination_label": "homelab-backup"},
    )

    result = await _status()

    assert result.status == "healthy"
    assert result.stale is False
    assert result.warning is None
    assert result.last_verified_at is not None
    assert result.last_offhost_at is not None
    assert result.offhost_destination_label == "homelab-backup"
    assert result.sha256_short == f"{SHA[:12]}..."


async def test_a_confirmed_but_old_backup_is_stale() -> None:
    age = settings.backup_stale_hours + 2
    await _created(age_hours=age)
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA}, age_hours=age)
    await _record(
        "backup.remote.copy",
        {"filename": DUMP, "sha256": SHA, "destination_label": "nas"},
        age_hours=age,
    )

    result = await _status()

    assert result.status == "stale"
    assert result.stale is True
    assert result.warning is not None
    assert str(settings.backup_stale_hours) in result.warning


async def test_evidence_timestamps_belong_to_the_matching_events() -> None:
    await _created(age_hours=3)
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA}, age_hours=2)
    await _record(
        "backup.remote.copy",
        {"filename": DUMP, "sha256": SHA, "destination_label": "nas"},
        age_hours=1,
    )

    result = await _status()

    assert result.last_backup_at is not None
    assert result.last_verified_at is not None
    assert result.last_offhost_at is not None
    # Ordering follows the run: created, then verified, then copied off-host.
    assert result.last_backup_at < result.last_verified_at < result.last_offhost_at


async def test_the_response_never_carries_transport_details() -> None:
    await _created()
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA})
    await _record(
        "backup.remote.copy",
        {"filename": DUMP, "sha256": SHA, "destination_label": "nas"},
    )

    result = await _status()

    # The schema is a fixed, reviewable field list: only an opaque label describes the destination.
    assert set(BackupStatusResponse.model_fields) == {
        "status",
        "last_backup_at",
        "last_verified_at",
        "last_offhost_at",
        "offhost_destination_label",
        "revision",
        "age_hours",
        "sha256_short",
        "stale",
        "warning",
    }
    serialized = result.model_dump_json()
    for forbidden in ("finspace-backup", "/srv", "id_backup", "known_hosts", "@", "ssh"):
        assert forbidden not in serialized


async def test_an_oversized_destination_label_is_truncated() -> None:
    await _created()
    await _record("backup.verified", {"filename": DUMP, "sha256": SHA})
    await _record(
        "backup.remote.copy",
        {"filename": DUMP, "sha256": SHA, "destination_label": "n" * 200},
    )

    result = await _status()

    assert result.offhost_destination_label is not None
    assert len(result.offhost_destination_label) <= 60
