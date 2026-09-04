"""Staged import artifacts are reclaimed only when the database proves they are not needed.

The staged upload is read exactly once, at upload time, to fill ``import_rows``; after that the
financial truth is PostgreSQL. So the question here is never "is this file old" but "can the
database account for it", and the interesting assertions are the refusals.

Most of the contract is proven without a database on purpose. The classification matrix and the
whole scan/decide/delete path are decided by the rows the database returns, not by how they were
fetched, so a stand-in session that returns those rows exercises the safety model exactly. Only the
statement itself needs real SQL; those few tests carry ``requires_database``.
"""

import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import import_staging
from app.services.import_staging import (
    ACTIVE,
    AMBIGUOUS,
    ORPHAN_WITHIN_GRACE,
    RECLAIMABLE_ORPHAN,
    RECLAIMABLE_TERMINAL,
    UNKNOWN,
)

MANAGED = "0123456789abcdef0123456789abcdef.csv"
OTHER_MANAGED = "fedcba9876543210fedcba9876543210.xlsx"
BATCH_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER_BATCH_ID = uuid.UUID("66666666-7777-8888-9999-aaaaaaaaaaaa")


class StubSession:
    """Returns the rows a real ``import_batches`` query would, and records that it was asked.

    ``_rows_by_stored_filename`` accumulates into a dict, so answering the first chunk with every
    row and later chunks with nothing produces exactly the same mapping a real database would,
    while still making the number of statements observable.
    """

    def __init__(self, rows: list[tuple[uuid.UUID, str, str]] | None = None) -> None:
        self._rows = rows or []
        self.statements = 0

    async def execute(self, statement: object) -> list[tuple[uuid.UUID, str, str]]:
        self.statements += 1
        return self._rows if self.statements == 1 else []


class BrokenSession:
    async def execute(self, statement: object) -> object:
        raise RuntimeError("database is unavailable")


@pytest.fixture
def staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "imports").resolve()
    root.mkdir(mode=0o700)
    monkeypatch.setattr(settings, "import_storage_path", root)
    monkeypatch.setattr(settings, "import_staging_reclaim_grace_hours", 72)
    monkeypatch.setattr(settings, "import_staging_reclaim_batch_size", 200)
    return root


def _write(root: Path, name: str, *, content: bytes = b"col\n1\n", age_hours: float = 0.0) -> Path:
    path = root / name
    path.write_bytes(content)
    if age_hours:
        when = (datetime.now(UTC) - timedelta(hours=age_hours)).timestamp()
        os.utime(path, (when, when))
    return path


async def _inspect(session: object) -> import_staging.StagingReport:
    return await import_staging.inspect_staging(session)  # type: ignore[arg-type]


async def _apply(session: object) -> import_staging.StagingReport:
    return await import_staging.reclaim_staging(session)  # type: ignore[arg-type]


def _by_name(report: import_staging.StagingReport, name: str) -> import_staging.Candidate:
    for candidate in report.candidates:
        if candidate.name == name:
            return candidate
    raise AssertionError(f"{name} was not scanned; saw {[c.name for c in report.candidates]}")


# --------------------------------------------------------------------------------------------
# Classification against the real import lifecycle
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["imported", "rolled_back", "cancelled"])
async def test_every_terminal_status_is_reclaimable(staging: Path, status: str) -> None:
    _write(staging, MANAGED)
    report = await _inspect(StubSession([(BATCH_ID, MANAGED, status)]))
    candidate = _by_name(report, MANAGED)
    assert candidate.classification == RECLAIMABLE_TERMINAL
    assert candidate.status == status
    assert candidate.batch_id == BATCH_ID
    assert report.reclaimable == 1


@pytest.mark.parametrize(
    "status",
    ["uploaded", "mapping_required", "parsed", "validated", "ready", "importing"],
)
async def test_a_resumable_import_is_never_reclaimed_however_old(
    staging: Path, status: str
) -> None:
    """Age is not a reason. A user may leave an import pending for weeks; nothing expires it."""
    _write(staging, MANAGED, age_hours=24 * 365)
    report = await _apply(StubSession([(BATCH_ID, MANAGED, status)]))
    assert _by_name(report, MANAGED).classification == ACTIVE
    assert report.reclaimed == 0
    assert report.skipped_active == 1
    assert (staging / MANAGED).exists()


async def test_a_status_this_contract_does_not_know_is_never_reclaimed(staging: Path) -> None:
    """If the lifecycle grows a state and this module does not, refusing is the safe reading."""
    _write(staging, MANAGED, age_hours=24 * 365)
    report = await _apply(StubSession([(BATCH_ID, MANAGED, "quarantined_by_a_future_release")]))
    assert _by_name(report, MANAGED).classification == UNKNOWN
    assert report.reclaimed == 0
    assert (staging / MANAGED).exists()


async def test_two_batches_claiming_one_artifact_are_ambiguous(staging: Path) -> None:
    _write(staging, MANAGED, age_hours=24 * 365)
    report = await _apply(
        StubSession([(BATCH_ID, MANAGED, "imported"), (OTHER_BATCH_ID, MANAGED, "ready")])
    )
    assert _by_name(report, MANAGED).classification == AMBIGUOUS
    assert report.skipped_ambiguous == 1
    assert report.reclaimed == 0
    assert (staging / MANAGED).exists()


# --------------------------------------------------------------------------------------------
# Orphans and the grace period
# --------------------------------------------------------------------------------------------


async def test_an_unreferenced_file_within_grace_is_retained(staging: Path) -> None:
    """An upload in flight has written its file and not yet committed its row."""
    _write(staging, MANAGED, age_hours=1)
    report = await _apply(StubSession())
    assert _by_name(report, MANAGED).classification == ORPHAN_WITHIN_GRACE
    assert report.skipped_orphan_within_grace == 1
    assert report.reclaimed == 0
    assert (staging / MANAGED).exists()


async def test_an_unreferenced_file_past_grace_is_reclaimed(staging: Path) -> None:
    _write(staging, MANAGED, age_hours=100)
    report = await _apply(StubSession())
    assert _by_name(report, MANAGED).classification == RECLAIMABLE_ORPHAN
    assert report.orphans_eligible == 1
    assert report.reclaimed == 1
    assert not (staging / MANAGED).exists()


async def test_the_grace_boundary_is_the_configured_value(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "import_staging_reclaim_grace_hours", 10)
    _write(staging, MANAGED, age_hours=9)
    _write(staging, OTHER_MANAGED, age_hours=11)
    report = await _inspect(StubSession())
    assert _by_name(report, MANAGED).classification == ORPHAN_WITHIN_GRACE
    assert _by_name(report, OTHER_MANAGED).classification == RECLAIMABLE_ORPHAN


async def test_a_name_outside_the_managed_namespace_is_never_reclaimed(staging: Path) -> None:
    """Legacy artifacts and the tracked .gitkeep are reported, never deleted."""
    legacy = [".gitkeep", "statement-2019.csv", "0123456789abcdef0123456789abcdef.pdf"]
    for name in legacy:
        _write(staging, name, age_hours=24 * 365)
    report = await _apply(StubSession())
    assert report.skipped_unknown == 3
    assert report.reclaimed == 0
    for name in legacy:
        assert _by_name(report, name).classification == UNKNOWN
        assert (staging / name).exists()


# --------------------------------------------------------------------------------------------
# Path confinement
# --------------------------------------------------------------------------------------------


async def test_a_directory_is_reported_and_never_descended(staging: Path) -> None:
    nested = staging / "0123456789abcdef0123456789abcdef.csv"
    nested.mkdir()
    inside = nested / OTHER_MANAGED
    inside.write_bytes(b"x")
    report = await _apply(StubSession())
    assert _by_name(report, nested.name).classification == UNKNOWN
    assert report.reclaimed == 0
    assert inside.exists()
    # Only the direct children of the managed root are ever considered.
    assert [candidate.name for candidate in report.candidates] == [nested.name]


async def test_a_symlink_is_refused_and_its_target_survives(staging: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"precious")
    link = staging / MANAGED
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("this platform does not permit creating symlinks")
    report = await _apply(StubSession([(BATCH_ID, MANAGED, "imported")]))
    assert _by_name(report, MANAGED).classification == UNKNOWN
    assert report.reclaimed == 0
    assert outside.exists()
    assert link.is_symlink()


async def test_a_traversing_stored_filename_cannot_reach_outside_the_root(
    staging: Path, tmp_path: Path
) -> None:
    """The database authorises a deletion; it never supplies the path."""
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"precious")
    session = StubSession(
        [
            (BATCH_ID, "../outside.csv", "imported"),
            (OTHER_BATCH_ID, str(tmp_path / "outside.csv"), "imported"),
        ]
    )
    report = await _apply(session)
    assert report.scanned == 0
    assert report.reclaimed == 0
    assert outside.exists()


@pytest.mark.parametrize("name", ["../outside.csv", "/etc/passwd", "sub/dir.csv", "..", "."])
def test_reclaim_refuses_any_name_that_is_not_one_path_component(
    staging: Path, tmp_path: Path, name: str
) -> None:
    """Three layered guards stand between a name and an unlink: it is a single component, it
    resolves inside the root, and it is still a regular file. Any one of them refusing is enough,
    and what must hold either way is that nothing around the root is touched."""
    sibling = tmp_path / "outside.csv"
    sibling.write_bytes(b"precious")
    with pytest.raises(ValueError):
        import_staging._reclaim_one(staging, name)
    assert sibling.exists()
    assert staging.is_dir()
    assert tmp_path.is_dir()


# --------------------------------------------------------------------------------------------
# Failure, idempotency and bounds
# --------------------------------------------------------------------------------------------


async def test_a_database_failure_deletes_nothing(staging: Path) -> None:
    _write(staging, MANAGED, age_hours=100)
    with pytest.raises(RuntimeError):
        await _apply(BrokenSession())
    assert (staging / MANAGED).exists()


async def test_inspection_never_deletes(staging: Path) -> None:
    _write(staging, MANAGED, age_hours=100)
    _write(staging, OTHER_MANAGED, age_hours=100)
    report = await _inspect(StubSession())
    assert report.applied is False
    assert report.reclaimable == 2
    assert report.reclaimed == 0
    assert (staging / MANAGED).exists()
    assert (staging / OTHER_MANAGED).exists()


async def test_apply_removes_the_candidate_and_nothing_else(staging: Path) -> None:
    _write(staging, MANAGED)
    _write(staging, OTHER_MANAGED)
    _write(staging, ".gitkeep", content=b"\n")
    report = await _apply(
        StubSession([(BATCH_ID, MANAGED, "imported"), (OTHER_BATCH_ID, OTHER_MANAGED, "ready")])
    )
    assert report.reclaimed == 1
    assert not (staging / MANAGED).exists()
    assert (staging / OTHER_MANAGED).exists()
    assert (staging / ".gitkeep").exists()


async def test_running_twice_converges(staging: Path) -> None:
    _write(staging, MANAGED, age_hours=100)
    first = await _apply(StubSession())
    second = await _apply(StubSession())
    assert first.reclaimed == 1
    assert second.scanned == 0
    assert second.reclaimed == 0
    assert second.failures == 0


async def test_an_artifact_that_vanished_first_is_not_a_failure(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The application's own inline unlink may win the race; the database already proved it safe."""
    _write(staging, MANAGED, age_hours=100)
    real_scan = import_staging._scan

    def scan_then_remove(root: Path) -> list[import_staging._Entry]:
        entries = real_scan(root)
        (root / MANAGED).unlink()
        return entries

    monkeypatch.setattr(import_staging, "_scan", scan_then_remove)
    report = await _apply(StubSession())
    assert report.already_absent == 1
    assert report.reclaimed == 0
    assert report.failures == 0


async def test_one_failure_does_not_widen_the_scope(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(staging, MANAGED, age_hours=100)
    _write(staging, OTHER_MANAGED, age_hours=100)
    real_reclaim = import_staging._reclaim_one

    def fail_the_first(root: Path, name: str) -> tuple[bool, int]:
        if name == MANAGED:
            raise OSError("permission denied")
        return real_reclaim(root, name)

    monkeypatch.setattr(import_staging, "_reclaim_one", fail_the_first)
    report = await _apply(StubSession())
    assert report.failures == 1
    assert report.reclaimed == 1
    assert (staging / MANAGED).exists()
    assert not (staging / OTHER_MANAGED).exists()


async def test_a_run_is_bounded_by_the_configured_batch_size(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "import_staging_reclaim_batch_size", 2)
    for index in range(5):
        _write(staging, f"{index:032x}.csv", age_hours=100)
    report = await _apply(StubSession())
    assert report.reclaimable == 5
    assert report.reclaimed == 2
    assert report.bounded is True
    assert len(os.listdir(staging)) == 3


async def test_the_status_lookup_is_issued_in_bounded_chunks(staging: Path) -> None:
    for index in range(1100):
        _write(staging, f"{index:032x}.csv", age_hours=1)
    session = StubSession()
    report = await _inspect(session)
    assert report.scanned == 1100
    # 1100 names at 500 per statement.
    assert session.statements == 3


async def test_a_missing_staging_root_is_an_empty_report_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "import_storage_path", tmp_path / "never-created")
    report = await _apply(StubSession())
    assert report.scanned == 0
    assert report.reclaimed == 0
    assert report.failures == 0


# --------------------------------------------------------------------------------------------
# Reporting: useful numbers, no user data
# --------------------------------------------------------------------------------------------


async def test_the_report_measures_disk_usage(staging: Path) -> None:
    _write(staging, MANAGED, content=b"x" * 1000)
    _write(staging, OTHER_MANAGED, content=b"y" * 500)
    report = await _inspect(
        StubSession([(BATCH_ID, MANAGED, "imported"), (OTHER_BATCH_ID, OTHER_MANAGED, "ready")])
    )
    assert report.scanned == 2
    assert report.total_bytes == 1500
    assert report.reclaimable == 1
    assert report.reclaimable_bytes == 1000


async def test_no_user_supplied_filename_reaches_the_report_or_the_log(
    staging: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``import_batches.filename`` can name a person or a bank; it is never selected at all."""
    _write(staging, MANAGED)
    with caplog.at_level(logging.INFO, logger="app.services.import_staging"):
        report = await _apply(StubSession([(BATCH_ID, MANAGED, "imported")]))
        import_staging.log_report(report)
    rendered = json.dumps(report.as_dict())
    for forbidden in ("Sberbank", 'filename":', "raw_data", "description"):
        assert forbidden not in rendered
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert MANAGED not in logged
    assert str(BATCH_ID) not in logged
    assert "scanned=1" in logged
    assert "reclaimed=1" in logged


# --------------------------------------------------------------------------------------------
# The command-line contract
# --------------------------------------------------------------------------------------------


def test_the_default_invocation_cannot_delete() -> None:
    from scripts import import_staging_reclaim

    assert import_staging_reclaim._parse_arguments([]).apply is False
    assert import_staging_reclaim._parse_arguments(["--json"]).apply is False
    assert import_staging_reclaim._parse_arguments(["--apply"]).apply is True


def test_apply_is_refused_while_reclamation_is_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The central switch refuses before anything is opened, and says how to still look."""
    from scripts import import_staging_reclaim

    monkeypatch.setattr(settings, "import_staging_reclaim_enabled", False)
    assert import_staging_reclaim.main(["--apply"]) == 2
    captured = capsys.readouterr()
    assert "IMPORT_STAGING_RECLAIM_ENABLED" in captured.err
    assert "without --apply to inspect" in captured.err


def test_the_human_summary_reports_usage_and_never_lists_a_batch() -> None:
    from scripts import import_staging_reclaim

    report = import_staging.StagingReport(root="/app/data/imports", grace_hours=72, batch_size=200)
    report.scanned = 4
    report.total_bytes = 4096
    report.reclaimable = 1
    report.reclaimable_bytes = 1024
    rendered = import_staging_reclaim._human(report)
    assert "files                       4" in rendered
    assert "reclaimable bytes           1024" in rendered
    assert "inspect (dry run)" in rendered
    assert "batch_id" not in rendered


# --------------------------------------------------------------------------------------------
# Real SQL against the real table
# --------------------------------------------------------------------------------------------


@pytest.mark.requires_database
async def test_the_lookup_matches_a_real_import_batch_row(staging: Path) -> None:
    """Proves the statement itself: the right table, the right three columns, the IN filter."""
    from sqlalchemy import delete

    from app.db.models.imports import ImportBatch
    from app.db.models.users import User, Workspace
    from app.db.session import AsyncSessionFactory

    marker = uuid.uuid4()
    async with AsyncSessionFactory() as session:
        user = User(
            email=f"staging-{marker}@example.com",
            normalized_email=f"staging-{marker}@example.com",
            display_name="Staging Test",
            timezone="Europe/Amsterdam",
        )
        session.add(user)
        await session.flush()
        workspace = Workspace(
            name="Staging Workspace",
            base_currency="RUB",
            timezone="Europe/Amsterdam",
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.flush()
        committed = ImportBatch(
            workspace_id=workspace.id,
            created_by=user.id,
            filename="Sberbank Ivan Petrov 2026.csv",
            stored_filename=MANAGED,
            file_type="csv",
            file_size=12,
            file_sha256="a" * 64,
            status="imported",
        )
        pending = ImportBatch(
            workspace_id=workspace.id,
            created_by=user.id,
            filename="Another Statement.csv",
            stored_filename=OTHER_MANAGED,
            file_type="xlsx",
            file_size=12,
            file_sha256="b" * 64,
            status="ready",
        )
        session.add_all([committed, pending])
        await session.commit()
        committed_id = committed.id

    try:
        _write(staging, MANAGED)
        _write(staging, OTHER_MANAGED)
        async with AsyncSessionFactory() as session:
            report = await import_staging.reclaim_staging(session)
        assert _by_name(report, MANAGED).classification == RECLAIMABLE_TERMINAL
        assert _by_name(report, MANAGED).batch_id == committed_id
        assert _by_name(report, OTHER_MANAGED).classification == ACTIVE
        assert report.reclaimed == 1
        assert not (staging / MANAGED).exists()
        assert (staging / OTHER_MANAGED).exists()
        # The user-supplied filename is not even selected, so it cannot reach the report.
        assert "Sberbank" not in json.dumps(report.as_dict())
    finally:
        async with AsyncSessionFactory() as session:
            await session.execute(
                delete(ImportBatch).where(ImportBatch.workspace_id == workspace.id)
            )
            await session.commit()
