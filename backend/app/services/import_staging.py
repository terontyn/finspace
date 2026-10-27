"""Reclamation of staged import artifacts under ``data/imports``.

After a committed import the financial truth is PostgreSQL: ``transactions``, ``import_batches``
and ``import_rows``. The staged upload is read exactly once — at upload time, to populate
``import_rows`` — and is never read again. ``upload_import`` removes it on failure, and
``commit_import`` and ``cancel_import`` unlink it inline once the batch reaches a terminal state.
So what this reclaims is not routine growth: it is what a crash left behind between the database
commit and that unlink, or between writing the upload and committing its row at all.

That is why nothing here deletes by age alone. A file is removed only when the database proves it
is no longer needed, or when it is provably unowned *and* older than the grace period. Everything
ambiguous — an unrecognized name, a directory, a symlink, a status this code does not know, two
rows claiming one artifact — is counted, reported and left exactly where it is.

The database is authoritative for *permission* to delete; the filesystem is authoritative for the
*path*. Candidate paths come only from enumerating the managed root, never from concatenating a
stored name onto it, so a malformed ``stored_filename`` cannot address anything outside it.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.imports import ImportBatch

logger = logging.getLogger("app.services.import_staging")

# The real lifecycle, read from app/services/imports.py. Terminal states are the ones the staged
# file is provably no longer needed in, and the ones the service itself already unlinks at.
TERMINAL_STATUSES = frozenset({"imported", "rolled_back", "cancelled"})
# Anything a user can still resume, plus the transient state a crashed commit leaves behind.
# `importing` is deliberately here: a stuck import is ambiguous, and ambiguity never deletes.
ACTIVE_STATUSES = frozenset(
    {"uploaded", "mapping_required", "parsed", "validated", "ready", "importing"}
)

# upload_import names every artifact `f"{uuid4().hex}.{extension}"`. This pattern is the only
# evidence available for a file the database does not reference at all, so orphan reclamation
# requires it. A file the database *does* reference is judged by its row instead.
MANAGED_NAME = re.compile(r"^[0-9a-f]{32}\.(?:csv|xlsx)\Z")

RECLAIMABLE_TERMINAL = "reclaimable_terminal"
RECLAIMABLE_ORPHAN = "reclaimable_orphan"
ACTIVE = "active"
ORPHAN_WITHIN_GRACE = "orphan_within_grace"
AMBIGUOUS = "ambiguous"
UNKNOWN = "unknown"

RECLAIMABLE = frozenset({RECLAIMABLE_TERMINAL, RECLAIMABLE_ORPHAN})

_STATUS_QUERY_CHUNK = 500


@dataclass(frozen=True)
class Candidate:
    """One entry in the managed root, with the reason for its classification.

    ``name`` is the stored filename — an opaque uuid hex — never the user-supplied
    ``import_batches.filename``, which can carry a person's or a bank's name.
    """

    name: str
    size_bytes: int
    age_hours: float
    classification: str
    reason: str
    batch_id: uuid.UUID | None = None
    status: str | None = None


@dataclass
class StagingReport:
    """What the managed root holds, and what a run did or would do to it."""

    root: str
    applied: bool = False
    grace_hours: int = 0
    batch_size: int = 0
    bounded: bool = False
    scanned: int = 0
    total_bytes: int = 0
    reclaimable: int = 0
    reclaimable_bytes: int = 0
    reclaimed: int = 0
    reclaimed_bytes: int = 0
    already_absent: int = 0
    skipped_active: int = 0
    skipped_orphan_within_grace: int = 0
    skipped_ambiguous: int = 0
    skipped_unknown: int = 0
    orphans_eligible: int = 0
    failures: int = 0
    candidates: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "applied": self.applied,
            "grace_hours": self.grace_hours,
            "batch_size": self.batch_size,
            "bounded": self.bounded,
            "scanned": self.scanned,
            "total_bytes": self.total_bytes,
            "reclaimable": self.reclaimable,
            "reclaimable_bytes": self.reclaimable_bytes,
            "reclaimed": self.reclaimed,
            "reclaimed_bytes": self.reclaimed_bytes,
            "already_absent": self.already_absent,
            "skipped_active": self.skipped_active,
            "skipped_orphan_within_grace": self.skipped_orphan_within_grace,
            "skipped_ambiguous": self.skipped_ambiguous,
            "skipped_unknown": self.skipped_unknown,
            "orphans_eligible": self.orphans_eligible,
            "failures": self.failures,
            "candidates": [
                {
                    "name": candidate.name,
                    "size_bytes": candidate.size_bytes,
                    "age_hours": round(candidate.age_hours, 2),
                    "classification": candidate.classification,
                    "reason": candidate.reason,
                    "batch_id": str(candidate.batch_id) if candidate.batch_id else None,
                    "status": candidate.status,
                }
                for candidate in self.candidates
            ],
        }


def _managed_root() -> Path:
    """The one directory this module may ever touch, fully resolved."""
    return settings.import_storage_path.resolve()


@dataclass(frozen=True)
class _Entry:
    name: str
    size_bytes: int
    modified_at: datetime
    is_regular_file: bool
    reason: str


def _scan(root: Path) -> list[_Entry]:
    """List the direct children of the managed root. Never recurses, never follows a link.

    A symlink is refused rather than resolved: resolving one is precisely how a cleanup escapes the
    directory it was confined to. Subdirectories are not part of the layout upload_import creates,
    so they are reported and never descended into.
    """
    entries: list[_Entry] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        name = child.name
        try:
            stat_result = child.lstat()
        except OSError:
            entries.append(
                _Entry(name, 0, datetime.now(UTC), False, "entry could not be inspected")
            )
            continue
        if child.is_symlink():
            entries.append(_Entry(name, 0, datetime.now(UTC), False, "entry is a symlink"))
            continue
        if child.is_dir():
            entries.append(_Entry(name, 0, datetime.now(UTC), False, "entry is a directory"))
            continue
        if not child.is_file():
            entries.append(_Entry(name, 0, datetime.now(UTC), False, "entry is not a regular file"))
            continue
        entries.append(
            _Entry(
                name,
                stat_result.st_size,
                datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
                True,
                "",
            )
        )
    return entries


async def _rows_by_stored_filename(
    session: AsyncSession, names: list[str]
) -> dict[str, list[tuple[uuid.UUID, str]]]:
    """Map each stored filename to every batch row claiming it.

    Queried in chunks so one run against a large directory issues bounded statements. A name with
    more than one row is returned as such: the caller treats that as ambiguous rather than picking
    one, because there is no correct way to pick.
    """
    rows: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for start in range(0, len(names), _STATUS_QUERY_CHUNK):
        chunk = names[start : start + _STATUS_QUERY_CHUNK]
        result = await session.execute(
            select(ImportBatch.id, ImportBatch.stored_filename, ImportBatch.status).where(
                ImportBatch.stored_filename.in_(chunk)
            )
        )
        for batch_id, stored_filename, status in result:
            rows.setdefault(stored_filename, []).append((batch_id, status))
    return rows


def classify(
    entry: _Entry,
    claims: list[tuple[uuid.UUID, str]],
    *,
    now: datetime,
    grace_hours: int,
) -> Candidate:
    """Decide one entry's fate. Pure, so every branch is testable without a database.

    Order matters: filesystem shape first (an unusual entry is never reasoned about further), then
    the database claim, and only in its absence the managed-name pattern plus age.
    """
    age_hours = max((now - entry.modified_at).total_seconds() / 3600.0, 0.0)

    if not entry.is_regular_file:
        return Candidate(entry.name, 0, age_hours, UNKNOWN, entry.reason)

    if len(claims) > 1:
        return Candidate(
            entry.name,
            entry.size_bytes,
            age_hours,
            AMBIGUOUS,
            f"{len(claims)} import batches claim this artifact",
        )

    if claims:
        batch_id, status = claims[0]
        if status in TERMINAL_STATUSES:
            return Candidate(
                entry.name,
                entry.size_bytes,
                age_hours,
                RECLAIMABLE_TERMINAL,
                f"batch is {status}; rows are durable in PostgreSQL",
                batch_id,
                status,
            )
        if status in ACTIVE_STATUSES:
            return Candidate(
                entry.name,
                entry.size_bytes,
                age_hours,
                ACTIVE,
                f"batch is {status} and can still be resumed",
                batch_id,
                status,
            )
        # A status this code does not recognise means the lifecycle moved and this module did not.
        # Refusing is the only safe reading.
        return Candidate(
            entry.name,
            entry.size_bytes,
            age_hours,
            UNKNOWN,
            "batch status is not recognised by this reclamation contract",
            batch_id,
            status,
        )

    if MANAGED_NAME.match(entry.name) is None:
        # No row and not a name this application ever creates: not ours to delete. Legacy
        # artifacts and the tracked .gitkeep land here deliberately.
        return Candidate(
            entry.name,
            entry.size_bytes,
            age_hours,
            UNKNOWN,
            "no import batch references it and the name is outside the managed namespace",
        )

    if age_hours < grace_hours:
        # An upload in flight has written its file but has not committed its row yet. The grace
        # period is what makes orphan reclamation race-free, so it must stay far longer than any
        # upload can take.
        return Candidate(
            entry.name,
            entry.size_bytes,
            age_hours,
            ORPHAN_WITHIN_GRACE,
            "unreferenced, but younger than the grace period",
        )

    return Candidate(
        entry.name,
        entry.size_bytes,
        age_hours,
        RECLAIMABLE_ORPHAN,
        "unreferenced by any import batch and older than the grace period",
    )


def _tally(report: StagingReport, candidate: Candidate) -> None:
    report.scanned += 1
    report.total_bytes += candidate.size_bytes
    if candidate.classification in RECLAIMABLE:
        report.reclaimable += 1
        report.reclaimable_bytes += candidate.size_bytes
        if candidate.classification == RECLAIMABLE_ORPHAN:
            report.orphans_eligible += 1
    elif candidate.classification == ACTIVE:
        report.skipped_active += 1
    elif candidate.classification == ORPHAN_WITHIN_GRACE:
        report.skipped_orphan_within_grace += 1
    elif candidate.classification == AMBIGUOUS:
        report.skipped_ambiguous += 1
    else:
        report.skipped_unknown += 1


def _reclaim_one(root: Path, name: str) -> tuple[bool, int]:
    """Delete one classified-safe artifact. Returns (deleted, bytes).

    Re-checked immediately before unlinking rather than trusting the scan: between classification
    and deletion an entry could have been replaced by a symlink or a directory. A file that is
    simply gone is not a failure — the application's own inline unlink may have won the race, and
    the database already proved the artifact is not needed.
    """
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise ValueError("refusing a candidate name that is not a single path component")
    target = root / name
    if target.parent != root:
        raise ValueError("refusing a candidate outside the managed root")
    try:
        stat_result = target.lstat()
    except FileNotFoundError:
        return False, 0
    if target.is_symlink() or not target.is_file():
        raise ValueError("refusing a candidate that is no longer a regular file")
    target.unlink(missing_ok=True)
    return True, stat_result.st_size


async def inspect_staging(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> StagingReport:
    """Classify everything in the managed root without touching a single byte."""
    return await _run(session, apply=False, now=now)


async def reclaim_staging(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> StagingReport:
    """Classify, then delete only the artifacts the database proved are no longer needed."""
    return await _run(session, apply=True, now=now)


async def _run(
    session: AsyncSession,
    *,
    apply: bool,
    now: datetime | None,
) -> StagingReport:
    moment = now or datetime.now(UTC)
    grace_hours = settings.import_staging_reclaim_grace_hours
    batch_size = settings.import_staging_reclaim_batch_size
    root = _managed_root()
    report = StagingReport(
        root=str(root), applied=apply, grace_hours=grace_hours, batch_size=batch_size
    )

    if not root.is_dir():
        # Nothing has been uploaded on this host yet. An empty report is the honest answer.
        return report

    entries = _scan(root)
    named = [entry.name for entry in entries if entry.is_regular_file]
    # The database is read after the directory listing on purpose: a row committed in between
    # classifies its file by status instead of leaving it to look unreferenced. Any exception here
    # propagates before a single deletion is attempted.
    claims = await _rows_by_stored_filename(session, named)

    for entry in entries:
        candidate = classify(entry, claims.get(entry.name, []), now=moment, grace_hours=grace_hours)
        report.candidates.append(candidate)
        _tally(report, candidate)

    if not apply:
        return report

    reclaimed = 0
    for candidate in report.candidates:
        if candidate.classification not in RECLAIMABLE:
            continue
        if reclaimed >= batch_size:
            # Bounded per run by design: a sweep that never ends is a sweep nobody can supervise.
            report.bounded = True
            break
        try:
            deleted, size_bytes = _reclaim_one(root, candidate.name)
        except (OSError, ValueError):
            # One artifact failing never widens the scope of the run; the rest are still eligible.
            report.failures += 1
            logger.warning(
                "import_staging_reclaim_failed "
                f"root={root} name={candidate.name} "
                f"classification={candidate.classification}"
            )
            continue
        reclaimed += 1
        if deleted:
            report.reclaimed += 1
            report.reclaimed_bytes += size_bytes
        else:
            report.already_absent += 1

    return report


def log_report(report: StagingReport) -> None:
    """One structured line, counts only.

    Nothing user-supplied is logged: not ``import_batches.filename``, not a row, not a column
    value. Stored filenames and batch ids are opaque uuids and stay in the report, not the log.
    """
    logger.info(
        "import_staging_reclaim_finished "
        f"applied={report.applied} "
        f"scanned={report.scanned} "
        f"reclaimable={report.reclaimable} "
        f"reclaimed={report.reclaimed} "
        f"reclaimed_bytes={report.reclaimed_bytes} "
        f"already_absent={report.already_absent} "
        f"skipped_active={report.skipped_active} "
        f"skipped_orphan_within_grace={report.skipped_orphan_within_grace} "
        f"skipped_ambiguous={report.skipped_ambiguous} "
        f"skipped_unknown={report.skipped_unknown} "
        f"orphans_eligible={report.orphans_eligible} "
        f"failures={report.failures} "
        f"bounded={report.bounded} "
        f"total_bytes={report.total_bytes} "
        f"reclaimable_bytes={report.reclaimable_bytes}"
    )
