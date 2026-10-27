"""The performance smoke is only worth having if it fails when it should.

The repository's own numbers pass, so the tests that matter here feed the gate measurements it
should reject: a page that costs more per row than its bound, a scenario that gets more expensive
as the workspace grows, a page that ignores its limit. The real run against PostgreSQL is exercised
through the command line, because that is what a release gate will invoke.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from scripts import performance_smoke
from scripts.performance_smoke import (
    Bound,
    Measurement,
    PerformanceSmokeError,
    QueryCounter,
    SmokeReport,
)

SMOKE_SOURCE = Path(performance_smoke.__file__).read_text(encoding="utf-8")


def _measurement(
    scenario: str, dataset: int, returned: int, queries: int, allowed: int = 999
) -> Measurement:
    return Measurement(scenario, dataset, returned, queries, 1.0, allowed)


# --------------------------------------------------------------------------------------------
# The query counter
# --------------------------------------------------------------------------------------------


class _FakeSyncEngine:
    pass


class _FakeEngine:
    def __init__(self) -> None:
        self.sync_engine = _FakeSyncEngine()


def test_the_counter_counts_only_inside_the_measured_block() -> None:
    """Seeding happens outside the block and must never land in a scenario's count."""
    counter = QueryCounter(_FakeEngine())
    counter._record(None, None, "SELECT 1 -- setup", None, None, None)
    assert counter.count == 0
    with counter:
        counter._record(None, None, "SELECT 2 -- measured", None, None, None)
        counter._record(None, None, "SELECT 3 -- measured", None, None, None)
    assert counter.count == 2
    counter._record(None, None, "SELECT 4 -- teardown", None, None, None)
    assert counter.count == 2


def test_entering_the_block_forgets_the_previous_scenario() -> None:
    counter = QueryCounter(_FakeEngine())
    with counter:
        counter._record(None, None, "SELECT 1", None, None, None)
    with counter:
        counter._record(None, None, "SELECT 2", None, None, None)
    assert counter.count == 1


def test_the_counter_keeps_statements_but_never_parameters() -> None:
    """Bound parameters carry the values a report must never print."""
    counter = QueryCounter(_FakeEngine())
    with counter:
        counter._record(
            None,
            None,
            "SELECT * FROM transactions WHERE counterparty = $1",
            ("a real payee name",),
            None,
            None,
        )
    assert counter.statements == ["SELECT * FROM transactions WHERE counterparty = $1"]
    assert "a real payee name" not in "".join(counter.statements)


# --------------------------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------------------------


def test_a_flat_bound_does_not_grow_with_the_page() -> None:
    bound = Bound(6)
    assert bound.allowed(10) == 6
    assert bound.allowed(1000) == 6


def test_a_linear_bound_states_its_slope() -> None:
    bound = Bound(25, per_row=3.3)
    assert bound.allowed(0) == 25
    assert bound.allowed(100) == 355


def test_the_transaction_bound_is_labelled_as_a_known_defect() -> None:
    """A bound that encodes current behaviour must say so wherever it is reported."""
    assert performance_smoke.TRANSACTION_PAGE_BOUND.note
    assert "N+1" in performance_smoke.TRANSACTION_PAGE_BOUND.note
    measurement = _measurement("transactions:first-page-25", 2000, 25, 82)
    measurement.note = performance_smoke.TRANSACTION_PAGE_BOUND.note
    report = SmokeReport()
    report.measurements.append(measurement)
    assert "known defect" in performance_smoke.render(report)
    assert measurement.as_dict()["known_defect"] == performance_smoke.TRANSACTION_PAGE_BOUND.note


# --------------------------------------------------------------------------------------------
# What the gate must reject
# --------------------------------------------------------------------------------------------


class _StubClient:
    """Answers one request with a controllable payload, status and query count."""

    def __init__(
        self, counter: QueryCounter, *, items: int, queries: int, status: int = 200
    ) -> None:
        self._counter = counter
        self._items = items
        self._queries = queries
        self._status = status

    def get(self, path: str, headers: dict[str, str] | None = None) -> "_StubClient":
        for index in range(self._queries):
            self._counter._record(None, None, f"SELECT {index}", None, None, None)
        return self

    @property
    def status_code(self) -> int:
        return self._status

    def json(self) -> dict[str, object]:
        return {"items": [{"id": index} for index in range(self._items)]}


def _run_one(*, items: int, queries: int, bound: Bound, expect: int | None, status: int = 200):
    report = SmokeReport()
    counter = QueryCounter(_FakeEngine())
    client = _StubClient(counter, items=items, queries=queries, status=status)
    measurement = performance_smoke.measure(
        report,
        counter,
        client,
        scenario="probe",
        dataset=2000,
        path="/api/v1/probe",
        headers={},
        bound=bound,
        expect_returned=expect,
    )
    return report, measurement


def test_exceeding_the_query_bound_fails_the_scenario() -> None:
    report, measurement = _run_one(items=25, queries=200, bound=Bound(10), expect=25)
    assert measurement.status == "fail"
    assert report.status == "fail"
    assert "200 queries exceeds the bound of 10" in measurement.detail


def test_staying_inside_the_query_bound_passes() -> None:
    report, measurement = _run_one(items=25, queries=4, bound=Bound(10), expect=25)
    assert measurement.status == "pass"
    assert report.status == "pass"
    assert report.failures == []


def test_a_page_that_ignores_its_limit_fails() -> None:
    """If a list stops bounding its page, the smoke must not quietly measure the whole table."""
    report, measurement = _run_one(items=2000, queries=4, bound=Bound(10), expect=50)
    assert measurement.status == "fail"
    assert "expected 50 items, got 2000" in measurement.detail
    assert report.status == "fail"


def test_a_non_200_response_fails_the_scenario() -> None:
    report, measurement = _run_one(items=0, queries=1, bound=Bound(10), expect=None, status=500)
    assert measurement.status == "fail"
    assert "HTTP 500" in measurement.detail
    assert report.status == "fail"


def test_the_catastrophe_guard_fails_a_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(performance_smoke, "CATASTROPHE_MS", 0)
    report, measurement = _run_one(items=1, queries=1, bound=Bound(10), expect=1)
    assert measurement.status == "fail"
    assert "guard" in measurement.detail
    assert report.status == "fail"


# --------------------------------------------------------------------------------------------
# The N+1 detectors
# --------------------------------------------------------------------------------------------


def test_a_flat_scenario_passes_the_slope_check() -> None:
    report = SmokeReport()
    performance_smoke.assert_bounded_slope(
        report,
        _measurement("probe", 2000, 25, 4),
        _measurement("probe", 2000, 100, 4),
        name="probe",
        max_extra_per_row=0.0,
    )
    assert report.failures == []
    assert "0.00 queries per extra row" in report.observations[0]


def test_one_query_per_extra_row_fails_a_flat_expectation() -> None:
    """5 queries for 25 rows and 105 for 100 is the regression this gate exists to catch."""
    report = SmokeReport()
    performance_smoke.assert_bounded_slope(
        report,
        _measurement("probe", 2000, 25, 5),
        _measurement("probe", 2000, 100, 105),
        name="probe",
        max_extra_per_row=0.0,
    )
    assert report.failures
    assert "1.33 queries per extra row" in report.failures[0]


def test_a_worsening_slope_fails_even_against_the_known_defect_bound() -> None:
    report = SmokeReport()
    performance_smoke.assert_bounded_slope(
        report,
        _measurement("probe", 2000, 25, 82),
        _measurement("probe", 2000, 100, 700),
        name="probe",
        max_extra_per_row=performance_smoke.TRANSACTION_PAGE_QUERIES_PER_ROW,
    )
    assert report.failures


def test_two_pages_of_the_same_size_cannot_be_compared() -> None:
    report = SmokeReport()
    performance_smoke.assert_bounded_slope(
        report,
        _measurement("probe", 2000, 25, 4),
        _measurement("probe", 2000, 25, 4),
        name="probe",
        max_extra_per_row=0.0,
    )
    assert report.failures


def test_growing_more_expensive_with_the_dataset_fails() -> None:
    """Same page, ten times the history: more statements means the request walks the history."""
    report = SmokeReport()
    report.measurements.append(_measurement("probe", performance_smoke.SMALL_DATASET, 25, 4))
    report.measurements.append(_measurement("probe", performance_smoke.LARGE_DATASET, 25, 40))
    performance_smoke._compare_datasets(report)
    assert report.failures
    assert "40 queries at" in report.failures[0]


def test_a_flat_scenario_survives_ten_times_the_dataset() -> None:
    report = SmokeReport()
    report.measurements.append(_measurement("probe", performance_smoke.SMALL_DATASET, 25, 4))
    report.measurements.append(_measurement("probe", performance_smoke.LARGE_DATASET, 25, 4))
    performance_smoke._compare_datasets(report)
    assert report.failures == []
    assert report.observations


def test_differing_row_counts_are_refused_rather_than_compared() -> None:
    report = SmokeReport()
    report.measurements.append(_measurement("probe", performance_smoke.SMALL_DATASET, 25, 4))
    report.measurements.append(_measurement("probe", performance_smoke.LARGE_DATASET, 50, 4))
    performance_smoke._compare_datasets(report)
    assert report.failures
    assert "not like for like" in report.failures[0]


# --------------------------------------------------------------------------------------------
# Isolation and output safety
# --------------------------------------------------------------------------------------------


def test_the_temporary_database_is_dropped_when_a_phase_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.engine import make_url

    created: list[str] = []
    dropped: list[str] = []

    async def fake_create(base_url: object, name: str) -> None:
        created.append(name)

    async def fake_drop(base_url: object, name: str, run_id: uuid.UUID) -> None:
        dropped.append(name)

    def explode(environment: dict[str, str]) -> None:
        raise PerformanceSmokeError("simulated migration failure")

    monkeypatch.setattr(performance_smoke, "_create_database", fake_create)
    monkeypatch.setattr(performance_smoke, "_drop_database", fake_drop)
    monkeypatch.setattr(performance_smoke, "_migrate", explode)

    base = make_url("postgresql+asyncpg://finspace:pw@postgres:5432/finspace_test")
    with pytest.raises(PerformanceSmokeError, match="simulated migration failure"):
        performance_smoke.run_smoke(base, quiet=True)
    assert created == dropped
    assert len(dropped) == 1


def test_dropping_refuses_a_database_this_run_did_not_create() -> None:
    import asyncio

    from sqlalchemy.engine import make_url

    base = make_url("postgresql+asyncpg://finspace:pw@postgres:5432/finspace_test")
    with pytest.raises(Exception) as raised:
        asyncio.run(performance_smoke._drop_database(base, "finspace", uuid.uuid4()))
    assert "pw" not in str(raised.value)


def test_the_fixtures_are_obviously_synthetic() -> None:
    """Nothing seeded may look like a real person, payee or account."""
    for prefix in (
        "synthetic-account-",
        "synthetic-category-",
        "synthetic-payee-",
        "synthetic-merchant-",
    ):
        assert prefix in SMOKE_SOURCE
    assert "@example.invalid" in SMOKE_SOURCE


def test_the_report_prints_identifiers_and_numbers_only() -> None:
    report = SmokeReport()
    report.measurements.append(_measurement("transactions:first-page-25", 2000, 25, 82, 107))
    rendered = performance_smoke.render(report)
    assert "queries=82/107" in rendered
    assert "duration_ms" in rendered
    for forbidden in ("counterparty", "@", "password", "Authorization", "Bearer"):
        assert forbidden not in rendered


def test_the_json_document_is_versioned_and_parseable() -> None:
    report = SmokeReport()
    report.measurements.append(_measurement("probe", 2000, 25, 4, 6))
    document = json.loads(json.dumps(report.as_dict()))
    assert document["version"] == 1
    assert document["status"] == "pass"
    assert set(document) == {
        "version",
        "status",
        "datasets",
        "catastrophe_timeout_ms",
        "scenarios",
        "failures",
        "observations",
    }
    scenario = document["scenarios"][0]
    assert scenario["queries"] == 4
    assert scenario["allowed_queries"] == 6
    assert isinstance(scenario["duration_ms"], float | int)


def test_a_failing_report_says_so_in_json() -> None:
    report = SmokeReport()
    report.failures.append("probe: too many queries")
    assert report.as_dict()["status"] == "fail"


def test_application_logs_are_moved_off_stdout_for_json_output() -> None:
    """The document has to be the only thing on stdout, and the lines still have to go somewhere."""
    import logging

    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    root.addHandler(handler)
    try:
        assert performance_smoke.send_application_logs_to_stderr() >= 1
        assert handler.stream is sys.stderr
    finally:
        root.removeHandler(handler)


def test_the_command_line_has_no_mutating_mode() -> None:
    parsed = performance_smoke._parse_arguments([])
    assert vars(parsed) == {"as_json": False}
    with pytest.raises(SystemExit):
        performance_smoke._parse_arguments(["--seed-production"])


# --------------------------------------------------------------------------------------------
# The real gate, against real PostgreSQL
# --------------------------------------------------------------------------------------------


def _base_url() -> object:
    return performance_smoke.resolve_base_url()


async def _temporary_databases() -> set[str]:
    connection = await asyncpg.connect(performance_smoke._admin_dsn(_base_url()))
    try:
        rows = await connection.fetch(
            "SELECT datname FROM pg_database WHERE datname LIKE 'finspace_test_%'"
        )
        return {row["datname"] for row in rows}
    finally:
        await connection.close()


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/performance_smoke.py", *arguments],
        cwd=str(Path(performance_smoke.__file__).resolve().parents[1]),
        env={**os.environ, "TESTING": "true"},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


@pytest.mark.requires_database
async def test_the_gate_passes_against_the_repository_baseline() -> None:
    before = await _temporary_databases()
    completed = _run_cli()
    after = await _temporary_databases()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "performance smoke: PASS" in completed.stdout
    for scenario in ("transactions:first-page-25", "accounts:balances", "summary:financial"):
        assert scenario in completed.stdout
    assert after == before, "the smoke left a temporary database behind"


@pytest.mark.requires_database
async def test_the_json_evidence_is_parseable_and_complete() -> None:
    before = await _temporary_databases()
    completed = _run_cli("--json")
    after = await _temporary_databases()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    document = json.loads(completed.stdout)
    assert document["version"] == 1
    assert document["status"] == "pass"
    assert document["failures"] == []
    assert document["datasets"] == {
        "small": performance_smoke.SMALL_DATASET,
        "large": performance_smoke.LARGE_DATASET,
    }

    scenarios = document["scenarios"]
    names = {item["scenario"] for item in scenarios}
    assert {"transactions:first-page-25", "accounts:list", "summary:financial"} <= names
    # Every scenario is measured at both workspace sizes.
    for name in names:
        sizes = {item["dataset"] for item in scenarios if item["scenario"] == name}
        assert sizes == {performance_smoke.SMALL_DATASET, performance_smoke.LARGE_DATASET}, name
    for item in scenarios:
        assert item["status"] == "pass"
        assert item["queries"] <= item["allowed_queries"]
        assert item["duration_ms"] >= 0

    # Evidence carries measurements, never content.
    rendered = json.dumps(document)
    for forbidden in ("Bearer", "password", "@example", "counterparty"):
        assert forbidden not in rendered
    assert after == before, "the smoke left a temporary database behind"


@pytest.mark.requires_database
async def test_the_bounded_scenarios_really_are_flat_across_dataset_sizes() -> None:
    """The scaling claim, read back out of the gate's own evidence."""
    completed = _run_cli("--json")
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    by_scenario: dict[str, dict[int, int]] = {}
    for item in document["scenarios"]:
        by_scenario.setdefault(item["scenario"], {})[item["dataset"]] = item["queries"]
    for scenario, sizes in by_scenario.items():
        small = sizes[performance_smoke.SMALL_DATASET]
        large = sizes[performance_smoke.LARGE_DATASET]
        assert large <= small, f"{scenario} cost more at the larger dataset"
