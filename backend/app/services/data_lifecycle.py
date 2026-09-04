"""What Finspace stores, why it grows, and who — if anyone — is allowed to reclaim it.

This module is diagnosis, never maintenance. It reads PostgreSQL catalog and statistics views and
a small set of managed directories, and it returns measurements. It deletes nothing, vacuums
nothing, analyzes nothing, and holds no data row in memory: no financial value, filename, email or
payload can reach its output because none is ever selected.

The registry below is the point of the exercise. Every table the application owns is classified
explicitly, so a table that appears in a future migration shows up as ``unclassified`` and forces a
lifecycle decision instead of quietly inheriting someone else's policy. Nothing here is coupled to
deletion — being classified reclaimable is a statement about who owns the retention, not a licence
for this code to act on it.

Measurements are a point-in-time operational view, not a transactionally frozen snapshot: writes
continue while the report runs, and row counts are PostgreSQL's own estimates.
"""

import logging
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger("app.services.data_lifecycle")

# --- lifecycle classes ---------------------------------------------------------------------------

FINANCIAL_TRUTH = "financial_truth"
OPERATIONAL_STATE = "operational_state"
DERIVED_HISTORY = "derived_history"
UNCLASSIFIED = "unclassified"

# --- retention owners ----------------------------------------------------------------------------

# Nothing in the application deletes these rows. That is a deliberate product position, not an
# oversight: removal happens only through an explicit user action in the product.
OWNER_USER_ACTION = "user_action_only"
# Append-only evidence and integration state. No automatic retention exists today; introducing one
# would be a product decision, and this report exists partly to inform it.
OWNER_NONE = "none_append_only"
# The categorization prune worker, configured by CATEGORIZATION_PRUNE_*.
OWNER_PRUNE_WORKER = "categorization_prune_worker"
# Removed by PostgreSQL when the parent row goes.
OWNER_CASCADE = "cascade_from_parent"


@dataclass(frozen=True)
class TablePolicy:
    lifecycle_class: str
    retention_owner: str
    note: str


def _truth(note: str) -> TablePolicy:
    return TablePolicy(FINANCIAL_TRUTH, OWNER_USER_ACTION, note)


def _state(note: str, owner: str = OWNER_NONE) -> TablePolicy:
    return TablePolicy(OPERATIONAL_STATE, owner, note)


# Every table the application owns, verified against Base.metadata by the test suite. A table
# missing here is reported as unclassified rather than assumed harmless.
TABLE_POLICIES: dict[str, TablePolicy] = {
    # --- financial and business source of truth ---------------------------------------------
    "accounts": _truth("Accounts; soft-deleted, never physically pruned."),
    "account_reconciliations": _truth("Statement reconciliation evidence."),
    "account_reconciliation_items": _truth("Per-line reconciliation evidence."),
    "budget_allocations": _truth("Budget lines; replaced wholesale when a period is re-planned."),
    "budget_periods": _truth("Budget periods."),
    "budget_plan_revisions": _truth("Immutable budget planning history."),
    "categories": _truth("Categories; soft-deleted."),
    "categorization_rules": _truth("User-authored categorization rules."),
    "categorization_rule_set_controls": _truth("Rule-set concurrency control per workspace."),
    "goals": _truth("Savings goals; soft-deleted."),
    "goal_contributions": _truth("Goal contribution history."),
    "import_batches": _truth(
        "Import provenance; required to explain where a transaction came from."
    ),
    "import_rows": _truth("Staged rows retained as import provenance and rollback evidence."),
    "month_closures": _truth("Hard month-close records."),
    "month_close_controls": _truth("Month-close concurrency control per workspace."),
    "month_close_revisions": _truth("Immutable close/reopen history."),
    "notification_settings": _truth("Per-workspace notification preferences."),
    "payees": _truth("Payees; soft-deleted."),
    "payee_aliases": _truth("Payee aliases."),
    "recurring_rules": _truth("Recurring rules; soft-deleted."),
    "recurring_rule_executions": _truth("Execution history proving what a rule created."),
    "system_metadata": _truth("Instance metadata; also asserted by backup verification."),
    "transactions": _truth("Financial source of truth; soft-deleted, never physically pruned."),
    "transaction_splits": _truth("Split lines; replaced wholesale when a transaction is edited."),
    "users": _truth("Identity; soft-deleted."),
    "workspaces": _truth("Workspaces; soft-deleted."),
    "workspace_members": _truth("Membership and roles."),
    # --- security, integration and evidence state -------------------------------------------
    "audit_log": _state(
        "Append-only audit evidence. Backup health reads backup.created/verified/remote.copy "
        "rows from here, so pruning it would blind that check."
    ),
    "auth_sessions": _state(
        "Refresh sessions. Rows are revoked or expired logically and are never deleted today, "
        "so this table grows with login activity."
    ),
    "automation_runs": _state("n8n automation run records with idempotency keys."),
    "categorization_apply_operations": _state("Idempotent bulk-apply requests and their outcome."),
    "categorization_apply_results": _state("Terminal per-item apply outcomes.", OWNER_CASCADE),
    "goal_command_results": _state("Idempotency results for goal commands."),
    "google_connections": _state("OAuth connection state; token ciphertext only."),
    "google_oauth_flows": _state("Short-lived OAuth flow state; expires logically."),
    "google_sheet_bindings": _state("Apps Script Bridge binding state and heartbeat."),
    "service_accounts": _state("Automation service accounts."),
    "service_api_keys": _state("Hashed ServiceKeys; never the secret itself."),
    "sync_conflicts": _state("Unresolved and resolved sync conflicts."),
    "sync_inbox": _state("Inbound sync deduplication records."),
    "sync_outbox": _state("Transactional outbox; delivered rows are retained, not deleted."),
    "sync_runs": _state("Sync run history."),
    "telegram_intents": _state("Telegram intent records with idempotency."),
    "telegram_links": _state("Telegram account links."),
    "telegram_link_codes": _state("Short-lived link codes; expire logically."),
    # --- derived, reclaimable by an existing owner -------------------------------------------
    "categorization_previews": TablePolicy(
        DERIVED_HISTORY,
        OWNER_PRUNE_WORKER,
        "Expired previews are physically reclaimed by the categorization-prune worker; the "
        "logical TTL is authoritative whether or not a row has been collected yet.",
    ),
    "categorization_preview_items": TablePolicy(
        DERIVED_HISTORY,
        OWNER_CASCADE,
        "Removed with their preview.",
    ),
}

# PostgreSQL and Alembic own these; they are not part of the application's lifecycle contract.
EXCLUDED_TABLES = frozenset({"alembic_version"})

# Long enough for a catalog query on a large database, short enough that a report can never sit on
# a connection indefinitely. Catalog and statistics reads do not scan user rows.
STATEMENT_TIMEOUT_MS = 30_000

# The report walks these directly rather than recursing: it wants sizes, not an inventory of names.
_MANAGED_DIRECTORY_SCAN_LIMIT = 100_000


@dataclass
class TableUsage:
    table: str
    lifecycle_class: str
    retention_owner: str
    note: str
    total_bytes: int
    data_bytes: int
    index_bytes: int
    toast_bytes: int
    row_estimate: int

    def as_dict(self) -> dict[str, object]:
        return {
            "table": self.table,
            "lifecycle_class": self.lifecycle_class,
            "retention_owner": self.retention_owner,
            "note": self.note,
            "total_bytes": self.total_bytes,
            "data_bytes": self.data_bytes,
            "index_bytes": self.index_bytes,
            "toast_bytes": self.toast_bytes,
            "row_estimate": self.row_estimate,
        }


@dataclass
class DirectoryUsage:
    """One managed directory, and how much of it was actually accounted for.

    ``readable`` and ``complete`` are separate on purpose. A directory can be perfectly readable
    and still yield a total that is only a lower bound — the scan hit its limit, or there was
    nested content this report deliberately does not descend into. Reporting that as a finished
    number is how a growth report ends up lying about growth, so an incomplete total is always
    marked and always warns.
    """

    path: str
    lifecycle_owner: str
    readable: bool
    complete: bool
    entries: int
    total_bytes: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "lifecycle_owner": self.lifecycle_owner,
            "readable": self.readable,
            "complete": self.complete,
            "entries": self.entries,
            "total_bytes": self.total_bytes,
            "detail": self.detail,
        }


@dataclass
class LifecycleReport:
    generated_at: str
    database_name: str = ""
    database_bytes: int = 0
    tables: list[TableUsage] = field(default_factory=list)
    directories: list[DirectoryUsage] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "partial" if self.warnings else "ok"

    def warn(self, code: str, detail: str) -> None:
        self.warnings.append({"code": code, "detail": detail})

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "generated_at": self.generated_at,
            "status": self.status,
            "database": {
                "name": self.database_name,
                "total_bytes": self.database_bytes,
                "table_count": len(self.tables),
                "row_estimate_total": sum(table.row_estimate for table in self.tables),
                "row_counts_are_estimates": True,
            },
            "tables": [table.as_dict() for table in self.tables],
            "filesystem": {"directories": [directory.as_dict() for directory in self.directories]},
            "warnings": list(self.warnings),
        }


# --- SQL -----------------------------------------------------------------------------------------

# Catalog and statistics only. n_live_tup is PostgreSQL's own estimate, maintained by autovacuum,
# so this never scans a user table. Ordering is total size then name, so two runs on an unchanged
# database produce the same document.
_TABLE_USAGE_SQL = text(
    """
    SELECT
        c.relname AS table_name,
        pg_total_relation_size(c.oid) AS total_bytes,
        pg_relation_size(c.oid) AS data_bytes,
        pg_indexes_size(c.oid) AS index_bytes,
        pg_total_relation_size(c.oid)
            - pg_relation_size(c.oid)
            - pg_indexes_size(c.oid) AS toast_bytes,
        COALESCE(s.n_live_tup, 0) AS row_estimate
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_user_tables AS s ON s.relid = c.oid
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
    ORDER BY pg_total_relation_size(c.oid) DESC, c.relname ASC
    """
)


async def _begin_read_only(session: AsyncSession) -> None:
    """Make the transaction incapable of writing, then bound how long it may run.

    READ ONLY has to be the first statement in the transaction, so it is issued before the
    timeout. Any DELETE, UPDATE, VACUUM or ALTER reaching PostgreSQL from here would be refused by
    the server rather than merely absent from the code.
    """
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))


def _scan_directory(path: Path, owner: str) -> DirectoryUsage:
    """Measure one managed directory without reading a byte of its contents.

    Never recurses and never follows a symlink: this is a size report, not a crawler, and a link
    is exactly how a size report escapes the tree it was pointed at.

    Anything the scan could not account for makes the total a lower bound rather than an answer:
    a hit scan limit, a subdirectory it declined to descend into, a child it could not stat. Those
    are marked incomplete, never rounded down into a confident number an operator might act on.
    """
    display = str(path)
    # Asked before exists(), which follows the link: a dangling symlink would otherwise look like
    # an innocent absent path instead of the refusal it is.
    if path.is_symlink():
        return DirectoryUsage(
            display, owner, False, False, 0, 0, "path is a symlink and was not followed"
        )
    if not path.exists():
        # Genuinely nothing here: zero is the true total and the scan is complete.
        return DirectoryUsage(display, owner, True, True, 0, 0, "path does not exist on this host")
    if not path.is_dir():
        return DirectoryUsage(display, owner, False, False, 0, 0, "path is not a directory")

    entries = 0
    total = 0
    unaccounted = 0
    nested = 0
    truncated = False
    try:
        for child in path.iterdir():
            if entries >= _MANAGED_DIRECTORY_SCAN_LIMIT:
                truncated = True
                break
            entries += 1
            # One lstat per child, and its failure is contained here. Asking is_symlink() and
            # is_dir() first would stat the child twice and let a single un-stattable entry abort
            # the whole directory, turning one bad file into "this directory is unreadable".
            try:
                child_stat = child.lstat()
            except OSError:
                unaccounted += 1
                continue
            mode = child_stat.st_mode
            if stat.S_ISLNK(mode):
                # lstat does not follow the link, and neither does this report, so its bytes are
                # deliberately not part of the total.
                unaccounted += 1
                continue
            if stat.S_ISDIR(mode):
                nested += 1
                continue
            if not stat.S_ISREG(mode):
                unaccounted += 1
                continue
            total += child_stat.st_size
    except PermissionError:
        return DirectoryUsage(display, owner, False, False, 0, 0, "not readable by this process")
    except OSError as error:
        return DirectoryUsage(
            display, owner, False, False, 0, 0, f"could not be read: {type(error).__name__}"
        )

    reasons = []
    if truncated:
        reasons.append(f"scan limit of {_MANAGED_DIRECTORY_SCAN_LIMIT} entries reached")
    if nested:
        reasons.append(f"{nested} nested directories were not descended into")
    if unaccounted:
        reasons.append(f"{unaccounted} entries could not be accounted for (symlink or unreadable)")
    if reasons:
        return DirectoryUsage(display, owner, True, False, entries, total, "; ".join(reasons))
    return DirectoryUsage(display, owner, True, True, entries, total, "fully measured")


def _managed_directories() -> list[tuple[Path, str]]:
    """The paths this process can meaningfully see from inside the backend container.

    ``backups`` is deliberately absent even though the container mounts it: the host keeps it 0700
    root-owned and the backend runs unprivileged, so anything measured here would be wrong. The
    host-side report covers backups instead.
    """
    return [
        (settings.import_storage_path, "F010 staged-import reclamation"),
        (Path("/app/data/acceptance"), "operator evidence; never auto-reclaimed"),
    ]


async def build_report(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> LifecycleReport:
    """Collect the whole report inside one read-only, time-bounded transaction."""
    moment = now or datetime.now(UTC)
    report = LifecycleReport(generated_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"))

    await _begin_read_only(session)

    report.database_name = str(await session.scalar(text("SELECT current_database()")) or "")
    report.database_bytes = int(
        await session.scalar(text("SELECT pg_database_size(current_database())")) or 0
    )

    seen: set[str] = set()
    for row in (await session.execute(_TABLE_USAGE_SQL)).all():
        name = str(row.table_name)
        if name in EXCLUDED_TABLES:
            continue
        seen.add(name)
        policy = TABLE_POLICIES.get(name)
        if policy is None:
            # Visible, never silent: a new table must get a lifecycle decision from a human.
            report.warn(
                "unclassified_table",
                f"{name} has no lifecycle classification; review before 1.0",
            )
            policy = TablePolicy(UNCLASSIFIED, OWNER_NONE, "no lifecycle classification")
        report.tables.append(
            TableUsage(
                table=name,
                lifecycle_class=policy.lifecycle_class,
                retention_owner=policy.retention_owner,
                note=policy.note,
                total_bytes=max(int(row.total_bytes or 0), 0),
                data_bytes=max(int(row.data_bytes or 0), 0),
                index_bytes=max(int(row.index_bytes or 0), 0),
                toast_bytes=max(int(row.toast_bytes or 0), 0),
                row_estimate=max(int(row.row_estimate or 0), 0),
            )
        )

    for missing in sorted(set(TABLE_POLICIES) - seen):
        # A classified table that is not in the database means the registry and the schema have
        # drifted; that is worth saying out loud rather than quietly omitting.
        report.warn(
            "classified_table_absent",
            f"{missing} is classified but is not present in this database",
        )

    for path, owner in _managed_directories():
        usage = _scan_directory(path, owner)
        report.directories.append(usage)
        if not usage.readable:
            report.warn("path_unreadable", f"{usage.path}: {usage.detail}")
        elif not usage.complete:
            # Readable but only partly accounted for. The number below it is a lower bound, and a
            # report that called that "ok" would be the exact failure this warning exists to stop.
            report.warn("path_partial", f"{usage.path}: totals are incomplete: {usage.detail}")

    return report


def log_report(report: LifecycleReport) -> None:
    """One structured line of counts and bytes; never a table's contents."""
    logger.info(
        "data_lifecycle_report "
        f"status={report.status} "
        f"database_bytes={report.database_bytes} "
        f"tables={len(report.tables)} "
        f"warnings={len(report.warnings)} "
        f"directories={len(report.directories)}"
    )
