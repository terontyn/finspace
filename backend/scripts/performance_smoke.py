"""Bounded performance smoke for the daily-use read paths.

    python scripts/performance_smoke.py            # human report
    python scripts/performance_smoke.py --json     # stable evidence document

This is a regression smoke, not a benchmark and not an SLA. Wall-clock numbers are recorded as
observations because they say something on the day they were taken and nothing about anyone
else's hardware; the assertions that gate a release are the deterministic ones — how many SQL
statements a request issues, how that number behaves when the page or the dataset grows, and
whether pagination actually bounds a page.

The interesting failure it exists to catch is the one nobody notices in review: a response builder
that quietly issues one more query per row. Every list scenario is therefore measured at two page
sizes against the same data, and the per-row slope is what is asserted.

Everything runs against a temporary database this process creates, migrates, seeds with synthetic
rows and drops — on failure as well as success. It never touches an existing database, and the
synthetic fixtures contain no name, address or description that resembles real data.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from sqlalchemy.engine import URL

from app.core.test_database_safety import (
    DatabaseSafetyError,
    validate_test_database_target,
)

# Sized for the shape of the product, not for a benchmark. A household accumulates a few thousand
# transactions over years across a handful of accounts, so the large fixture is roughly "several
# years in", and the small one is roughly "a few months in". The pair exists so a per-row cost can
# be seen; making them bigger would slow the gate without changing what it detects.
SMALL_DATASET = 200
LARGE_DATASET = 2000
ACCOUNTS = 6
CATEGORIES = 30
PAYEES = 40
# Roughly one transaction in ten is split, which is what makes the splits query path visible.
SPLIT_EVERY = 10

# Generous by design: this catches a deadlock or a quadratic blow-up, not a slow laptop. Observed
# development runs finish every scenario in well under a second, so this leaves more than an order
# of magnitude of headroom for slower or contended CI hardware.
CATASTROPHE_MS = 15_000


class PerformanceSmokeError(RuntimeError):
    """A gate failure that names a scenario and a bound, never a credential."""


def send_application_logs_to_stderr() -> int:
    """Keep stdout clean so ``--json`` output can be piped into something.

    The application configures its JSON log handler on stdout, and exercising the API through a
    TestClient makes it log a line per request — straight into the middle of the document. The
    handler is repointed rather than silenced, so the lines are still there to read on stderr.
    """
    import logging

    moved = 0
    for handler in logging.getLogger().handlers:
        if getattr(handler, "stream", None) is sys.stdout:
            handler.setStream(sys.stderr)  # type: ignore[attr-defined]
            moved += 1
    return moved


# The transaction page is measurably linear in the page size today: transaction_response issues a
# splits query for every row and then loads that row's account and category individually. Measured
# on this baseline at 3.28, 3.15 and 3.13 queries per returned row for pages of 25, 100 and 200.
#
# This bound records that behaviour; it is NOT a target. Changing a hot path is out of scope for a
# measurement PR, so the number is written down where a review can see it, the report labels every
# scenario that uses it, and any worsening still fails. Fixing the N+1 makes the slope drop, which
# this upper bound welcomes — and the bound should be tightened in the same change.
TRANSACTION_PAGE_QUERIES_PER_ROW = 3.3


# ---------------------------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------------------------


class QueryCounter:
    """Count SQL statements issued while the block runs.

    Uses the same ``before_cursor_execute`` hook the payee tests already use, so this is the
    project's own mechanism rather than a new one. Only the statement text is retained — never the
    bound parameters, which carry the values the report must never print.
    """

    def __init__(self, engine: Any) -> None:
        self._sync_engine = engine.sync_engine
        self.statements: list[str] = []
        self._counting = False

    def _record(self, *args: Any, **kwargs: Any) -> None:
        if self._counting:
            statement = args[2] if len(args) > 2 else ""
            self.statements.append(str(statement))

    def install(self) -> None:
        from sqlalchemy import event

        event.listen(self._sync_engine, "before_cursor_execute", self._record)

    def remove(self) -> None:
        from sqlalchemy import event

        event.remove(self._sync_engine, "before_cursor_execute", self._record)

    def __enter__(self) -> "QueryCounter":
        # Fixture setup happens outside the block, so seeding never lands in a measured count.
        self.statements = []
        self._counting = True
        return self

    def __exit__(self, *_: object) -> None:
        self._counting = False

    @property
    def count(self) -> int:
        return len(self.statements)


# ---------------------------------------------------------------------------------------------
# Scenario contract
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bound:
    """What a scenario is allowed to cost.

    ``per_row`` is the slope that matters. Zero means the request must issue the same number of
    statements whether it returns ten rows or a hundred; a positive value is a deliberate,
    documented statement that the current implementation is linear in the page.
    """

    base_queries: int
    per_row: float = 0.0
    note: str = ""

    def allowed(self, rows: int) -> int:
        return self.base_queries + int(self.per_row * rows)


TRANSACTION_PAGE_BOUND = Bound(
    25,
    per_row=TRANSACTION_PAGE_QUERIES_PER_ROW,
    note="known N+1: one splits query plus an account and category load per returned row",
)


@dataclass
class Measurement:
    scenario: str
    dataset: int
    returned: int
    queries: int
    duration_ms: float
    allowed_queries: int
    status: str = "pass"
    detail: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "dataset": self.dataset,
            "returned": self.returned,
            "queries": self.queries,
            "allowed_queries": self.allowed_queries,
            "duration_ms": round(self.duration_ms, 1),
            "status": self.status,
            "detail": self.detail,
            "known_defect": self.note,
        }


@dataclass
class SmokeReport:
    dataset_small: int = SMALL_DATASET
    dataset_large: int = LARGE_DATASET
    measurements: list[Measurement] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "fail" if self.failures else "pass"

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "status": self.status,
            "datasets": {"small": self.dataset_small, "large": self.dataset_large},
            "catastrophe_timeout_ms": CATASTROPHE_MS,
            "scenarios": [item.as_dict() for item in self.measurements],
            "failures": list(self.failures),
            "observations": list(self.observations),
        }


# ---------------------------------------------------------------------------------------------
# Temporary database
# ---------------------------------------------------------------------------------------------


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _admin_dsn(base_url: URL) -> str:
    return _render(base_url.set(drivername="postgresql", database="postgres"))


async def _create_database(base_url: URL, name: str) -> None:
    connection = await asyncpg.connect(_admin_dsn(base_url))
    try:
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


async def _drop_database(base_url: URL, name: str, run_id: uuid.UUID) -> None:
    """Drop only the database this run created; the name embeds this run's own identifier."""
    validate_test_database_target(
        _render(base_url.set(database=name)),
        environ={**os.environ, "TESTING": "true", "ENVIRONMENT": "test"},
        expected_run_id=str(run_id),
    )
    if name != f"finspace_test_{run_id.hex}":
        raise PerformanceSmokeError("refusing to drop a database this run did not create")
    connection = await asyncpg.connect(_admin_dsn(base_url))
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await connection.execute(f'DROP DATABASE "{name}"')
    finally:
        await connection.close()


def resolve_base_url() -> URL:
    value = os.environ.get("TEST_DATABASE_URL", "")
    if not value:
        raise PerformanceSmokeError("TEST_DATABASE_URL is required for the performance smoke")
    return validate_test_database_target(
        value,
        environ={**os.environ, "TESTING": "true", "ENVIRONMENT": "test"},
    ).url


def _migrate(environment: dict[str, str]) -> None:
    completed = subprocess.run(
        ["alembic", "upgrade", "head"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PerformanceSmokeError(
            f"could not migrate the smoke database (exit {completed.returncode})"
        )


# ---------------------------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------------------------


def seed(workspace_id: uuid.UUID, user_id: uuid.UUID, transactions: int) -> None:
    """Deterministic synthetic rows. No name here resembles a real person, payee or account.

    Written through the ORM so every mixin default — timestamps, version, soft-delete — is filled
    the way the application fills it, rather than by hand-rolled INSERTs that could drift.

    Deliberately on its own engine rather than the application's: the TestClient runs the app in
    its own event loop and its pooled connections belong to that loop, so seeding through the same
    pool from ``asyncio.run`` would hand a future to the wrong loop. A NullPool engine opened and
    disposed here shares nothing.
    """
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db.models.accounts import Account
    from app.db.models.categories import Category
    from app.db.models.payees import Payee
    from app.db.models.transactions import FinancialTransaction, TransactionSplit

    seed_engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    session_factory = async_sessionmaker(seed_engine, expire_on_commit=False)

    async def _write() -> None:
        async with session_factory() as session:
            start = datetime(2024, 1, 1, tzinfo=UTC)
            accounts = [
                Account(
                    workspace_id=workspace_id,
                    name=f"synthetic-account-{index:02d}",
                    account_type="cash",
                    currency="RUB",
                    opening_balance=Decimal("0"),
                    opening_balance_at=start,
                )
                for index in range(ACCOUNTS)
            ]
            categories = [
                Category(
                    workspace_id=workspace_id,
                    name=f"synthetic-category-{index:02d}",
                    category_type="expense" if index % 2 else "income",
                )
                for index in range(CATEGORIES)
            ]
            payees = [
                Payee(
                    workspace_id=workspace_id,
                    name=f"synthetic-payee-{index:02d}",
                    created_by=user_id,
                    updated_by=user_id,
                )
                for index in range(PAYEES)
            ]
            session.add_all([*accounts, *categories, *payees])
            await session.flush()

            rows: list[FinancialTransaction] = []
            for index in range(transactions):
                account = accounts[index % ACCOUNTS]
                category = categories[index % CATEGORIES]
                payee = payees[index % PAYEES]
                rows.append(
                    FinancialTransaction(
                        workspace_id=workspace_id,
                        occurred_at=start + timedelta(hours=index),
                        transaction_type="expense" if index % 3 else "income",
                        amount=Decimal("100.00") + Decimal(index % 500),
                        currency="RUB",
                        account_id=account.id,
                        category_id=category.id,
                        payee_id=payee.id,
                        counterparty=f"synthetic-merchant-{index % PAYEES:02d}",
                        status="confirmed",
                        source="manual",
                        created_by=user_id,
                    )
                )
            session.add_all(rows)
            await session.flush()

            splits: list[TransactionSplit] = []
            for index, row in enumerate(rows):
                if index % SPLIT_EVERY:
                    continue
                half = (row.amount / 2).quantize(Decimal("0.0001"))
                splits.extend(
                    TransactionSplit(
                        transaction_id=row.id,
                        category_id=categories[(index + part) % CATEGORIES].id,
                        amount=half,
                    )
                    for part in range(2)
                )
            session.add_all(splits)
            await session.commit()

    async def _write_then_dispose() -> None:
        try:
            await _write()
        finally:
            await seed_engine.dispose()

    asyncio.run(_write_then_dispose())


def register_operator(client: Any) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
    """One synthetic user and workspace, through the normal registration path.

    Registration is setup, not a measured scenario: its Argon2 hashing would dominate any timing
    and says nothing about the read paths this gate is about.
    """
    email = f"perf-{uuid.uuid4().hex}@example.invalid"
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Performance Smoke",
            "password": "correct horse battery staple",
            "workspace_name": "Performance Smoke Workspace",
            "base_currency": "RUB",
            "timezone": "Europe/Amsterdam",
        },
    )
    if response.status_code != 201:
        raise PerformanceSmokeError(
            f"could not create the synthetic operator ({response.status_code})"
        )
    payload = response.json()
    return (
        {
            "Authorization": f"Bearer {payload['access_token']}",
            "X-Workspace-ID": payload["workspace"]["id"],
        },
        uuid.UUID(payload["workspace"]["id"]),
        uuid.UUID(payload["user"]["id"]),
    )


# ---------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------


def _count_items(payload: object) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return len(payload["items"])
    return 1


def measure(
    report: SmokeReport,
    counter: QueryCounter,
    client: Any,
    *,
    scenario: str,
    dataset: int,
    path: str,
    headers: dict[str, str],
    bound: Bound,
    expect_returned: int | None = None,
) -> Measurement:
    started = time.perf_counter()
    with counter:
        response = client.get(path, headers=headers)
    duration_ms = (time.perf_counter() - started) * 1000
    queries = counter.count

    if response.status_code != 200:
        measurement = Measurement(
            scenario,
            dataset,
            0,
            queries,
            duration_ms,
            bound.allowed(0),
            "fail",
            f"HTTP {response.status_code}",
        )
        report.measurements.append(measurement)
        report.failures.append(f"{scenario}: HTTP {response.status_code}")
        return measurement

    returned = _count_items(response.json())
    allowed = bound.allowed(returned)
    measurement = Measurement(
        scenario, dataset, returned, queries, duration_ms, allowed, note=bound.note
    )

    if expect_returned is not None and returned != expect_returned:
        measurement.status = "fail"
        measurement.detail = f"expected {expect_returned} items, got {returned}"
        report.failures.append(f"{scenario}: {measurement.detail}")
    elif queries > allowed:
        measurement.status = "fail"
        measurement.detail = f"{queries} queries exceeds the bound of {allowed} for {returned} rows"
        report.failures.append(f"{scenario}: {measurement.detail}")
    elif duration_ms > CATASTROPHE_MS:
        measurement.status = "fail"
        measurement.detail = f"took {duration_ms:.0f} ms, over the {CATASTROPHE_MS} ms guard"
        report.failures.append(f"{scenario}: {measurement.detail}")

    report.measurements.append(measurement)
    return measurement


def assert_bounded_slope(
    report: SmokeReport,
    small: Measurement,
    large: Measurement,
    *,
    name: str,
    max_extra_per_row: float,
) -> None:
    """The N+1 detector: how much does one more returned row cost in statements?"""
    extra_rows = large.returned - small.returned
    if extra_rows <= 0:
        report.failures.append(f"{name}: the two pages returned the same number of rows")
        return
    slope = (large.queries - small.queries) / extra_rows
    report.observations.append(
        f"{name}: {small.queries} queries for {small.returned} rows, "
        f"{large.queries} for {large.returned} — {slope:.2f} queries per extra row"
    )
    if slope > max_extra_per_row:
        report.failures.append(
            f"{name}: {slope:.2f} queries per extra row exceeds {max_extra_per_row:.2f}"
        )


# ---------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------


def run_smoke(base_url: URL, *, quiet: bool = False) -> SmokeReport:
    run_id = uuid.uuid4()
    name = f"finspace_test_{run_id.hex}"
    database_url = _render(base_url.set(database=name))
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "ENVIRONMENT": "development",
        "TESTING": "true",
        "TEST_RUN_ID": str(run_id),
        "ALLOW_DEV_AUTH_HEADERS": "false",
    }

    def say(text: str) -> None:
        if not quiet:
            print(text, flush=True)

    asyncio.run(_create_database(base_url, name))
    try:
        _migrate(environment)
        # Imported only now: the application binds its engine at import time, so the environment
        # has to name the smoke database before anything under app.* is loaded.
        os.environ.update(
            {
                "DATABASE_URL": database_url,
                "ENVIRONMENT": "development",
                "TESTING": "true",
                "TEST_RUN_ID": str(run_id),
            }
        )
        from fastapi.testclient import TestClient

        from app.db.session import engine
        from app.main import app

        if quiet:
            send_application_logs_to_stderr()

        report = SmokeReport()
        counter = QueryCounter(engine)
        counter.install()
        try:
            with TestClient(app) as client:
                # Two workspaces in one database, one an order of magnitude larger than the other.
                # Every scenario is measured against both: the returned page is the same size, so
                # any difference in statement count is the request reacting to how much data the
                # workspace holds. Sharing the database also means a missing workspace filter would
                # show up as the wrong row count rather than passing quietly.
                for dataset in (SMALL_DATASET, LARGE_DATASET):
                    headers, workspace_id, user_id = register_operator(client)
                    say(f"seeding {dataset} synthetic transactions")
                    seed(workspace_id, user_id, dataset)
                    _run_scenarios(report, counter, client, headers, dataset)
                _compare_datasets(report)
        finally:
            counter.remove()
        return report
    finally:
        asyncio.run(_drop_database(base_url, name, run_id))


def _compare_datasets(report: SmokeReport) -> None:
    """The scaling assertion: ten times the data must not cost more statements.

    Each scenario returns the same number of rows in both workspaces, so a difference here can
    only come from the request doing more work because the workspace holds more history — which is
    the shape of an accidental full-table walk or a per-history-row lookup.
    """
    by_scenario: dict[str, dict[int, Measurement]] = {}
    for item in report.measurements:
        by_scenario.setdefault(item.scenario, {})[item.dataset] = item
    for scenario, sizes in sorted(by_scenario.items()):
        small = sizes.get(SMALL_DATASET)
        large = sizes.get(LARGE_DATASET)
        if small is None or large is None:
            continue
        if small.returned != large.returned:
            report.failures.append(
                f"{scenario}: returned {small.returned} rows at {SMALL_DATASET} and "
                f"{large.returned} at {LARGE_DATASET}; the comparison is not like for like"
            )
            continue
        report.observations.append(
            f"{scenario}: {small.queries} queries at {SMALL_DATASET} transactions, "
            f"{large.queries} at {LARGE_DATASET}"
        )
        if large.queries > small.queries:
            report.failures.append(
                f"{scenario}: {large.queries} queries at {LARGE_DATASET} transactions against "
                f"{small.queries} at {SMALL_DATASET} for the same {small.returned} rows"
            )


def _run_scenarios(
    report: SmokeReport,
    counter: QueryCounter,
    client: Any,
    headers: dict[str, str],
    dataset: int,
) -> None:

    # The primary hot path, measured at two page sizes against the same data. The base allowance
    # covers the count, the page, the batched payee lookup and the handful of distinct accounts
    # and categories the identity map has to load once each.
    small_page = measure(
        report,
        counter,
        client,
        scenario="transactions:first-page-25",
        dataset=dataset,
        path="/api/v1/transactions?limit=25",
        headers=headers,
        bound=TRANSACTION_PAGE_BOUND,
        expect_returned=25,
    )
    large_page = measure(
        report,
        counter,
        client,
        scenario="transactions:first-page-100",
        dataset=dataset,
        path="/api/v1/transactions?limit=100",
        headers=headers,
        bound=TRANSACTION_PAGE_BOUND,
        expect_returned=100,
    )
    # Locked at the behaviour reconnaissance actually found: one statement per returned row, not
    # zero. Tightening this is what a fix for the splits N+1 would do, and the gate would then
    # fail loudly until the bound is lowered with it — which is the point.
    assert_bounded_slope(
        report,
        small_page,
        large_page,
        name="transactions:first-page scaling",
        max_extra_per_row=TRANSACTION_PAGE_BOUND.per_row,
    )

    measure(
        report,
        counter,
        client,
        scenario="transactions:filtered-page",
        dataset=dataset,
        path="/api/v1/transactions?limit=25&transaction_type=expense&search=merchant",
        headers=headers,
        bound=TRANSACTION_PAGE_BOUND,
        expect_returned=25,
    )
    measure(
        report,
        counter,
        client,
        scenario="transactions:pagination-cap",
        dataset=dataset,
        # The route caps limit at 200; a page must never widen just because the table did.
        path="/api/v1/transactions?limit=200",
        headers=headers,
        bound=TRANSACTION_PAGE_BOUND,
        expect_returned=200,
    )
    measure(
        report,
        counter,
        client,
        scenario="accounts:list",
        dataset=dataset,
        path="/api/v1/accounts?limit=50",
        headers=headers,
        bound=Bound(6),
        expect_returned=ACCOUNTS,
    )
    measure(
        report,
        counter,
        client,
        scenario="accounts:balances",
        dataset=dataset,
        path="/api/v1/accounts/balances",
        headers=headers,
        bound=Bound(6),
        expect_returned=ACCOUNTS,
    )
    measure(
        report,
        counter,
        client,
        scenario="summary:financial",
        dataset=dataset,
        path="/api/v1/financial-summary",
        headers=headers,
        bound=Bound(6),
    )
    measure(
        report,
        counter,
        client,
        scenario="categories:list",
        dataset=dataset,
        path="/api/v1/categories?limit=50",
        headers=headers,
        bound=Bound(6),
        expect_returned=CATEGORIES,
    )
    measure(
        report,
        counter,
        client,
        scenario="imports:history",
        dataset=dataset,
        path="/api/v1/imports?limit=50",
        headers=headers,
        bound=Bound(6),
        expect_returned=0,
    )


# ---------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------


def render(report: SmokeReport) -> str:
    lines = [
        f"performance smoke: {report.status.upper()}",
        f"  dataset              {report.dataset_large} synthetic transactions",
        f"  catastrophe guard    {CATASTROPHE_MS} ms per scenario",
        "",
        "  Durations are observations from this run, not a service level objective.",
        "",
    ]
    for item in report.measurements:
        lines.append(f"  {item.scenario}")
        lines.append(
            f"    dataset={item.dataset} returned={item.returned} "
            f"queries={item.queries}/{item.allowed_queries} "
            f"duration_ms={item.duration_ms:.1f}  {item.status.upper()}"
        )
        if item.detail:
            lines.append(f"    {item.detail}")
        if item.note:
            lines.append(f"    known defect: {item.note}")
    if report.observations:
        lines += ["", "  OBSERVATIONS"]
        lines += [f"    {line}" for line in report.observations]
    if report.failures:
        lines += ["", "  FAILURES"]
        lines += [f"    {line}" for line in report.failures]
    return "\n".join(lines)


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bounded performance smoke for the daily-use read paths.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the stable evidence document instead of the human report",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    try:
        base_url = resolve_base_url()
        report = run_smoke(base_url, quiet=arguments.as_json)
    except (PerformanceSmokeError, DatabaseSafetyError) as error:
        print(f"performance smoke: FAIL: {error}", file=sys.stderr)
        return 1

    if arguments.as_json:
        # stdout carries the document alone; the application logs there too, so nothing else
        # may be printed in this mode.
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(render(report))
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
