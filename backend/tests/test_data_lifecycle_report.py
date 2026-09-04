"""The lifecycle report has to be trustworthy in two directions.

It must never touch anything — no row read, no statement that could write — and it must never
quietly approve of a table nobody has classified. The tests below are mostly about those two
properties; the sizes themselves are PostgreSQL's, and one integration test confirms the catalog
queries really work against the real schema.
"""

import ast
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db import models  # noqa: F401 - registers every table on Base.metadata
from app.db.base import Base
from app.db.session import AsyncSessionFactory
from app.services import data_lifecycle
from app.services.data_lifecycle import (
    DERIVED_HISTORY,
    EXCLUDED_TABLES,
    FINANCIAL_TRUTH,
    OPERATIONAL_STATE,
    TABLE_POLICIES,
    UNCLASSIFIED,
)

LIFECYCLE_SOURCE = Path(data_lifecycle.__file__).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Row:
    table_name: str
    total_bytes: int
    data_bytes: int
    index_bytes: int
    toast_bytes: int
    row_estimate: int


class _Result:
    def __init__(self, rows: list[Row]) -> None:
        self._rows = rows

    def all(self) -> list[Row]:
        return self._rows


class StubSession:
    """Answers the catalog queries and records every statement the report issued."""

    def __init__(
        self, rows: list[Row] | None = None, *, database: str = "finspace", size: int = 4096
    ) -> None:
        self.rows = rows or []
        self.database = database
        self.size = size
        self.executed: list[str] = []

    async def execute(self, statement: object) -> object:
        sql = str(statement)
        self.executed.append(sql)
        if "pg_total_relation_size" in sql:
            return _Result(self.rows)
        return None

    async def scalar(self, statement: object) -> object:
        sql = str(statement)
        self.executed.append(sql)
        if "pg_database_size" in sql:
            return self.size
        return self.database


class BrokenSession:
    async def execute(self, statement: object) -> object:
        raise RuntimeError("database is unavailable")

    async def scalar(self, statement: object) -> object:
        raise RuntimeError("database is unavailable")


def _row(name: str, total: int = 8192) -> Row:
    return Row(name, total, total - 2048, 2048, 0, 10)


async def _build(session: object) -> data_lifecycle.LifecycleReport:
    return await data_lifecycle.build_report(
        session,  # type: ignore[arg-type]
        now=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------------------------------------
# The registry must track the real schema
# --------------------------------------------------------------------------------------------


def test_every_application_table_is_classified() -> None:
    """The point of the registry: a new migration cannot introduce an unreviewed lifecycle."""
    model_tables = set(Base.metadata.tables) - EXCLUDED_TABLES
    unclassified = sorted(model_tables - set(TABLE_POLICIES))
    assert unclassified == [], f"tables with no lifecycle classification: {unclassified}"


def test_the_registry_has_no_table_the_application_no_longer_owns() -> None:
    model_tables = set(Base.metadata.tables) - EXCLUDED_TABLES
    stale = sorted(set(TABLE_POLICIES) - model_tables)
    assert stale == [], f"classified tables that no longer exist: {stale}"


def test_every_policy_uses_a_known_class_and_names_an_owner() -> None:
    known = {FINANCIAL_TRUTH, OPERATIONAL_STATE, DERIVED_HISTORY}
    for table, policy in TABLE_POLICIES.items():
        assert policy.lifecycle_class in known, f"{table} has an unknown class"
        assert policy.retention_owner, f"{table} names no retention owner"
        assert policy.note, f"{table} has no explanation"


def test_financial_truth_is_never_owned_by_an_automatic_reclaimer() -> None:
    """Nothing may quietly acquire permission to prune financial history."""
    for table, policy in TABLE_POLICIES.items():
        if policy.lifecycle_class == FINANCIAL_TRUTH:
            assert policy.retention_owner == data_lifecycle.OWNER_USER_ACTION, table


def test_only_the_prune_worker_owns_automatic_row_reclamation() -> None:
    owned = sorted(
        table
        for table, policy in TABLE_POLICIES.items()
        if policy.retention_owner == data_lifecycle.OWNER_PRUNE_WORKER
    )
    assert owned == ["categorization_previews"]


# --------------------------------------------------------------------------------------------
# An unclassified table is visible, never silent
# --------------------------------------------------------------------------------------------


async def test_an_unknown_table_is_flagged_and_never_assumed_safe() -> None:
    session = StubSession([_row("transactions"), _row("some_future_table")])
    report = await _build(session)
    future = next(table for table in report.tables if table.table == "some_future_table")
    assert future.lifecycle_class == UNCLASSIFIED
    assert report.status == "partial"
    assert any(warning["code"] == "unclassified_table" for warning in report.warnings)
    assert "some_future_table" in report.warnings[0]["detail"]


async def test_a_classified_table_missing_from_the_database_is_reported() -> None:
    session = StubSession([_row("transactions")])
    report = await _build(session)
    codes = {warning["code"] for warning in report.warnings}
    assert "classified_table_absent" in codes
    assert report.status == "partial"


async def test_alembic_version_is_excluded_rather_than_flagged() -> None:
    session = StubSession([_row("alembic_version")])
    report = await _build(session)
    assert [table.table for table in report.tables] == []
    assert not any(warning["code"] == "unclassified_table" for warning in report.warnings)


# --------------------------------------------------------------------------------------------
# Read-only by construction
# --------------------------------------------------------------------------------------------


async def test_the_transaction_is_made_read_only_before_anything_else() -> None:
    session = StubSession([_row("transactions")])
    await _build(session)
    assert session.executed[0] == "SET TRANSACTION READ ONLY"
    assert "statement_timeout" in session.executed[1]


async def test_a_database_failure_propagates_rather_than_returning_a_partial_report() -> None:
    """No fallback guess: a report that cannot read the catalog must not exist at all."""
    with pytest.raises(RuntimeError):
        await _build(BrokenSession())


def _issued_statements() -> list[str]:
    """Every SQL string this module hands to ``text()``, read out of its syntax tree.

    Scanning the file as prose would be useless — a note saying "soft-deleted" contains DELETE.
    What matters is the statements actually sent to PostgreSQL, so those are what is collected.
    """
    statements: list[str] = []
    for node in ast.walk(ast.parse(LIFECYCLE_SOURCE)):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "text"):
            continue
        assert node.args, "text() was called without a statement"
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            statements.append(argument.value)
        elif isinstance(argument, ast.JoinedStr):
            literal = "".join(
                part.value
                for part in argument.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            statements.append(literal)
        else:  # pragma: no cover - a computed statement would itself be the finding
            raise AssertionError("a SQL statement is built dynamically")
    return statements


def test_the_module_issues_only_select_and_set_statements() -> None:
    statements = _issued_statements()
    assert statements, "no SQL was found; the extraction is broken"
    for statement in statements:
        first = statement.strip().split()[0].upper()
        assert first in {"SELECT", "SET"}, f"unexpected statement: {statement.strip()[:60]}"


@pytest.mark.parametrize(
    "forbidden",
    ["DELETE", "UPDATE", "INSERT", "TRUNCATE", "VACUUM", "REINDEX", "ANALYZE", "ALTER", "DROP"],
)
def test_no_maintenance_or_mutation_keyword_reaches_postgresql(forbidden: str) -> None:
    for statement in _issued_statements():
        assert forbidden not in statement.upper(), f"{forbidden} appears in an issued statement"


def test_row_counts_are_estimates_rather_than_counted() -> None:
    assert "n_live_tup" in LIFECYCLE_SOURCE
    assert "count(*)" not in LIFECYCLE_SOURCE.lower()


def test_the_query_reads_only_catalog_and_statistics_relations() -> None:
    statement = str(data_lifecycle._TABLE_USAGE_SQL)
    referenced = set(re.findall(r"(?:FROM|JOIN)\s+([a-z_.]+)", statement))
    assert referenced <= {"pg_class", "pg_namespace", "pg_stat_user_tables"}
    for business_table in ("transactions", "users", "accounts", "import_rows", "audit_log"):
        assert business_table not in statement


# --------------------------------------------------------------------------------------------
# Shape of the document
# --------------------------------------------------------------------------------------------


async def test_the_json_document_is_versioned_and_stable() -> None:
    session = StubSession([_row("transactions", 40960), _row("audit_log", 20480)])
    document = (await _build(session)).as_dict()
    assert document["version"] == 1
    assert set(document) == {
        "version",
        "generated_at",
        "status",
        "database",
        "tables",
        "filesystem",
        "warnings",
    }
    assert document["generated_at"] == "2026-09-04T12:00:00Z"
    database = document["database"]
    assert isinstance(database, dict)
    assert database["row_counts_are_estimates"] is True
    # Serialisable, with integer byte values throughout.
    rendered = json.loads(json.dumps(document))
    for table in rendered["tables"]:
        for key in ("total_bytes", "data_bytes", "index_bytes", "toast_bytes", "row_estimate"):
            assert isinstance(table[key], int)
            assert table[key] >= 0


async def test_ordering_follows_the_query_and_is_reproducible() -> None:
    rows = [_row("transactions", 40960), _row("audit_log", 20480), _row("users", 8192)]
    first = await _build(StubSession(rows))
    second = await _build(StubSession(rows))
    assert [table.table for table in first.tables] == ["transactions", "audit_log", "users"]
    assert first.as_dict() == second.as_dict()


async def test_negative_catalog_values_are_never_reported() -> None:
    """PostgreSQL will not return these, but a report of storage must not print a negative size."""
    session = StubSession([Row("transactions", -1, -1, -1, -1, -1)])
    report = await _build(session)
    table = report.tables[0]
    assert (table.total_bytes, table.data_bytes, table.index_bytes, table.row_estimate) == (
        0,
        0,
        0,
        0,
    )


# --------------------------------------------------------------------------------------------
# Managed directories
# --------------------------------------------------------------------------------------------


def test_a_readable_directory_is_measured_without_reading_contents(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_bytes(b"x" * 100)
    (tmp_path / "b.csv").write_bytes(b"y" * 50)
    usage = data_lifecycle._scan_directory(tmp_path, "test owner")
    assert usage.readable is True
    assert usage.entries == 2
    assert usage.total_bytes == 150


def test_a_nested_directory_is_counted_but_not_descended(tmp_path: Path) -> None:
    (tmp_path / "flat.csv").write_bytes(b"x" * 10)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "huge.csv").write_bytes(b"z" * 10_000)
    usage = data_lifecycle._scan_directory(tmp_path, "test owner")
    assert usage.total_bytes == 10
    assert "not descended" in usage.detail


def test_a_symlink_is_skipped_rather_than_followed(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    (target / "big.csv").write_bytes(b"z" * 5000)
    managed = tmp_path / "managed"
    managed.mkdir()
    try:
        (managed / "link").symlink_to(target / "big.csv")
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("this platform does not permit creating symlinks")
    usage = data_lifecycle._scan_directory(managed, "test owner")
    assert usage.total_bytes == 0
    assert "symlink" in usage.detail
    assert (target / "big.csv").exists()


def test_a_directory_that_is_itself_a_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "big.csv").write_bytes(b"z" * 5000)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("this platform does not permit creating symlinks")
    usage = data_lifecycle._scan_directory(link, "test owner")
    assert usage.readable is False
    assert usage.total_bytes == 0
    assert "symlink" in usage.detail


def test_a_missing_path_is_reported_as_missing_not_as_empty(tmp_path: Path) -> None:
    usage = data_lifecycle._scan_directory(tmp_path / "absent", "test owner")
    assert usage.readable is True
    assert usage.total_bytes == 0
    assert "does not exist" in usage.detail


async def test_an_unreadable_managed_path_warns_instead_of_claiming_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = data_lifecycle.DirectoryUsage(
        str(tmp_path), "test owner", False, 0, 0, "not readable by this process"
    )
    monkeypatch.setattr(data_lifecycle, "_scan_directory", lambda path, owner: blocked)
    report = await _build(StubSession([_row("transactions")]))
    assert any(warning["code"] == "path_unreadable" for warning in report.warnings)
    assert report.status == "partial"


def test_staged_import_classification_is_delegated_to_f010() -> None:
    """F010 owns active/terminal/orphan/ambiguous. This module must not re-decide any of it."""
    assert "F010" in LIFECYCLE_SOURCE
    for f010_class in ("reclaimable_terminal", "reclaimable_orphan", "orphan_within_grace"):
        assert f010_class not in LIFECYCLE_SOURCE


def test_the_report_depends_on_neither_n8n_nor_redis() -> None:
    """Core inspection must work on a host where n8n is absent or crash-looping."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(LIFECYCLE_SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("redis" in name or "n8n" in name for name in imported), sorted(imported)
    # And the module never reaches for either at runtime.
    for statement in _issued_statements():
        assert "n8n" not in statement.lower()


# --------------------------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------------------------


def test_there_is_no_apply_or_delete_mode() -> None:
    from scripts import data_lifecycle_report

    parsed = data_lifecycle_report._parse_arguments([])
    assert not hasattr(parsed, "apply")
    assert vars(parsed) == {"as_json": False, "top": 15}
    with pytest.raises(SystemExit):
        data_lifecycle_report._parse_arguments(["--apply"])


def test_a_database_failure_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import data_lifecycle_report

    async def broken() -> data_lifecycle.LifecycleReport:
        raise RuntimeError("database is unavailable")

    monkeypatch.setattr(data_lifecycle_report, "_collect", broken)
    assert data_lifecycle_report.main([]) == 1
    captured = capsys.readouterr()
    assert "failed" in captured.err
    assert captured.out == ""


def test_json_mode_puts_nothing_but_the_document_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The application logs to stdout, so a summary line there would make --json unparseable."""
    from scripts import data_lifecycle_report

    prepared = data_lifecycle.LifecycleReport(generated_at="2026-09-04T12:00:00Z")
    prepared.database_name = "finspace"
    prepared.database_bytes = 4096

    async def collect() -> data_lifecycle.LifecycleReport:
        return prepared

    monkeypatch.setattr(data_lifecycle_report, "_collect", collect)
    assert data_lifecycle_report.main(["--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["version"] == 1
    assert document["database"]["total_bytes"] == 4096


def test_the_human_summary_shows_sizes_and_owners_only() -> None:
    from scripts import data_lifecycle_report

    report = data_lifecycle.LifecycleReport(generated_at="2026-09-04T12:00:00Z")
    report.database_name = "finspace"
    report.database_bytes = 1048576
    report.tables.append(
        data_lifecycle.TableUsage(
            table="transactions",
            lifecycle_class=FINANCIAL_TRUTH,
            retention_owner=data_lifecycle.OWNER_USER_ACTION,
            note="Financial source of truth",
            total_bytes=524288,
            data_bytes=262144,
            index_bytes=262144,
            toast_bytes=0,
            row_estimate=1200,
        )
    )
    rendered = data_lifecycle_report.render(report, top=15)
    assert "1.0 MiB" in rendered
    assert "transactions" in rendered
    assert f"{FINANCIAL_TRUTH} / {data_lifecycle.OWNER_USER_ACTION}" in rendered
    assert "estimates" in rendered


def test_human_byte_rendering_is_iec() -> None:
    from scripts import data_lifecycle_report

    assert data_lifecycle_report.human_bytes(0) == "0 B"
    assert data_lifecycle_report.human_bytes(1024) == "1.0 KiB"
    assert data_lifecycle_report.human_bytes(1048576) == "1.0 MiB"


# --------------------------------------------------------------------------------------------
# Against the real database
# --------------------------------------------------------------------------------------------


@pytest.mark.requires_database
async def test_the_catalog_queries_work_against_the_real_schema() -> None:
    async with AsyncSessionFactory() as session, session.begin():
        report = await data_lifecycle.build_report(session)

    assert report.database_bytes > 0
    assert report.database_name
    names = {table.table for table in report.tables}
    assert "transactions" in names
    assert "audit_log" in names
    assert "alembic_version" not in names
    assert names == set(TABLE_POLICIES), "the live schema and the registry disagree"
    unclassified = [warning for warning in report.warnings if warning["code"] != "path_unreadable"]
    assert unclassified == [], f"unexpected lifecycle warnings: {unclassified}"
    for table in report.tables:
        assert table.total_bytes >= 0
        assert table.total_bytes >= table.index_bytes
        assert table.lifecycle_class != UNCLASSIFIED
    totals = [table.total_bytes for table in report.tables]
    assert totals == sorted(totals, reverse=True)


@pytest.mark.requires_database
async def test_no_stored_value_can_reach_the_report() -> None:
    """Plant values a leak would expose, then look for them in the finished document."""
    from app.db.models.imports import ImportBatch
    from app.db.models.users import User, Workspace

    marker = uuid.uuid4().hex[:12]
    email = f"lifecycle-{marker}@example.com"
    upload = f"Sberbank statement {marker}.csv"
    async with AsyncSessionFactory() as session:
        user = User(
            email=email,
            normalized_email=email,
            display_name=f"Lifecycle {marker}",
            timezone="Europe/Amsterdam",
        )
        session.add(user)
        await session.flush()
        workspace = Workspace(
            name=f"Lifecycle {marker}",
            base_currency="RUB",
            timezone="Europe/Amsterdam",
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.flush()
        session.add(
            ImportBatch(
                workspace_id=workspace.id,
                created_by=user.id,
                filename=upload,
                stored_filename=f"{uuid.uuid4().hex}.csv",
                file_type="csv",
                file_size=10,
                file_sha256="c" * 64,
                status="imported",
            )
        )
        await session.commit()

    async with AsyncSessionFactory() as session, session.begin():
        report = await data_lifecycle.build_report(session)
    rendered = json.dumps(report.as_dict())
    for planted in (marker, email, upload, "Sberbank", "example.com"):
        assert planted not in rendered
