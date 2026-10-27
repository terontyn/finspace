"""Prove the migration chain is sound and that a clean database can reach head.

    python scripts/validate_migrations.py                  # graph + real PostgreSQL (the gate)
    python scripts/validate_migrations.py --static-only    # graph only, no database
    python scripts/validate_migrations.py --expect-head <revision> --expect-count <n>

Two halves, deliberately separable:

*Graph.* Alembic's own ``ScriptDirectory`` is the authority — it is what will resolve the chain at
runtime, so re-implementing it would only prove this file agrees with itself. What is added on top
are the things Alembic merely *warns* about or does not consider policy: a duplicate revision id
(Alembic warns and then silently drops one of them), a branch label or cross-dependency this
project does not support, a filename that disagrees with the revision it declares.

*Database.* A uniquely named temporary database is created, migrated, inspected and dropped. It
never touches the production database or a developer's own: the name always embeds a uuid4 this
process just generated, and the existing test-database guard refuses anything else.

The expected head is not hard-coded. Pinning it here would mean editing this file with every
migration; instead the actual head is printed, and ``--expect-head``/``--expect-count`` let a
release gate pin the values it was reviewed against.

Downgrade is not part of this gate. Production rollback is restore-based, and asserting a downgrade
path here would imply support this project deliberately does not offer.
"""

import argparse
import ast
import asyncio
import os
import re
import subprocess
import sys
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL

from app.core.test_database_safety import (
    DatabaseSafetyError,
    validate_test_database_target,
)
from app.db import models  # noqa: F401 - registers every table on Base.metadata
from app.db.base import Base

# Checkpoints are chosen for what they exercise, not to repeat the fresh-install path: a clean
# upgrade to head already runs all seventeen migrations in order. Each of these starts a database
# at a released schema state and then migrates it forward, which is the operation an upgrade
# actually performs and the one where a migration that assumes an empty table would fail.
#
#   0001_foundation           the root; proves the chain is walkable from the very beginning
#   0005_apps_script_bridge   the Google provider boundary, and the runner's own cycle floor
#   0010_goals               mid-history, immediately before the payee relations in 0011
#   0016_audit_cause         the previous release-era head, and the last ALTER on a populated
#                            table before the current head
DEFAULT_CHECKPOINTS = (
    "0001_foundation",
    "0005_apps_script_bridge",
    "0010_goals",
    "0016_audit_cause",
)

ALEMBIC_VERSION_TABLE = "alembic_version"

_DSN_PATTERN = re.compile(r"[a-zA-Z0-9+.\-]+://\S*")


class MigrationGateError(RuntimeError):
    """A gate failure that is safe to print: phase and revisions, never a connection string."""


# ---------------------------------------------------------------------------------------------
# Secret-safe reporting
# ---------------------------------------------------------------------------------------------


def redact(message: str, *, secret: str | None = None) -> str:
    """Strip anything that could carry credentials out of a message before it is printed.

    Alembic and the drivers below happily include a DSN in an exception, and a gate that prints
    the failure verbatim would put the database password in CI output.
    """
    cleaned = _DSN_PATTERN.sub("<dsn redacted>", message)
    if secret:
        cleaned = cleaned.replace(secret, "<redacted>")
    return cleaned


# ---------------------------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RevisionFile:
    path: Path
    revision: str
    down_revision: str | None
    branch_labels: object
    depends_on: object


@dataclass
class GraphReport:
    head: str = ""
    root: str = ""
    revision_count: int = 0
    chain: list[str] = field(default_factory=list)


def _literal(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return "<not-a-literal>"


def scan_revision_files(versions: Path) -> list[RevisionFile]:
    """Read each revision's declarations without importing it.

    Parsing rather than importing keeps this usable on a deliberately broken fixture and keeps a
    migration's module-level code out of the validator's process.
    """
    if not versions.is_dir():
        raise MigrationGateError(f"versions directory is missing: {versions}")
    found: list[RevisionFile] = []
    for path in sorted(versions.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as error:
            raise MigrationGateError(f"{path.name} does not parse: {error.msg}") from error
        declared: dict[str, object] = {}
        for node in tree.body:
            # Revisions in this repository are annotated (`revision: str = "..."`); crafted
            # fixtures and older templates use a plain assignment. Both are read.
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    declared[node.target.id] = _literal(node.value)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    declared[target.id] = _literal(node.value)
        if "revision" not in declared:
            raise MigrationGateError(f"{path.name} declares no revision id")
        revision = declared["revision"]
        if not isinstance(revision, str) or not revision.strip():
            raise MigrationGateError(f"{path.name} has a malformed revision id")
        if "down_revision" not in declared:
            raise MigrationGateError(f"{path.name} declares no down_revision")
        down = declared["down_revision"]
        if down is not None and (not isinstance(down, str) or not down.strip()):
            raise MigrationGateError(f"{path.name} has a malformed down_revision")
        found.append(
            RevisionFile(
                path=path,
                revision=revision,
                down_revision=down,
                branch_labels=declared.get("branch_labels"),
                depends_on=declared.get("depends_on"),
            )
        )
    if not found:
        raise MigrationGateError(f"no revision files were found in {versions}")
    return found


def _load_script_directory(script_location: Path) -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(script_location))
    try:
        # Alembic reports a duplicate revision id and a dangling parent as warnings and then keeps
        # going, quietly losing a revision. Promoted here: a warning about the chain is a failure.
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            script = ScriptDirectory.from_config(config)
            # from_config does not read the revisions; the map is built lazily on first use, so a
            # cycle or a dangling parent would otherwise surface far from this guard.
            script.get_heads()
            return script
    except MigrationGateError:
        raise
    except Exception as error:
        raise MigrationGateError(
            f"the migration graph could not be loaded ({type(error).__name__}): {error}"
        ) from error


def validate_graph(script_location: Path) -> GraphReport:
    """Static validation of the whole chain. No database, no migration is executed."""
    files = scan_revision_files(script_location / "versions")

    seen: dict[str, Path] = {}
    for item in files:
        if item.revision in seen:
            # Alembic only warns about this and then drops one of the two, so a duplicate can
            # silently remove a migration from the chain. It is a hard failure here.
            raise MigrationGateError(
                f"duplicate revision id {item.revision!r} in "
                f"{seen[item.revision].name} and {item.path.name}"
            )
        seen[item.revision] = item.path
        if item.branch_labels is not None:
            raise MigrationGateError(
                f"{item.path.name} declares branch labels; this project supports one linear chain"
            )
        if item.depends_on is not None:
            raise MigrationGateError(
                f"{item.path.name} declares depends_on; this project supports one linear chain"
            )
        if item.path.stem != item.revision:
            raise MigrationGateError(
                f"{item.path.name} declares revision {item.revision!r}; "
                "the filename and the revision id must agree"
            )

    script = _load_script_directory(script_location)
    heads = list(script.get_heads())
    if len(heads) != 1:
        raise MigrationGateError(f"expected exactly one head, found {len(heads)}: {sorted(heads)}")
    bases = list(script.get_bases())
    if len(bases) != 1:
        raise MigrationGateError(
            f"expected exactly one root revision, found {len(bases)}: {sorted(bases)}"
        )

    head = heads[0]
    chain = [revision.revision for revision in script.walk_revisions("base", head)]
    if len(chain) != len(files):
        unreachable = sorted(set(seen) - set(chain))
        raise MigrationGateError(
            f"{len(unreachable)} revision(s) are not reachable from the head: {unreachable}"
        )

    return GraphReport(
        head=head,
        root=bases[0],
        revision_count=len(files),
        chain=list(reversed(chain)),
    )


# ---------------------------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------------------------


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _admin_dsn(base_url: URL) -> str:
    return _render(base_url.set(drivername="postgresql", database="postgres"))


async def _create_database(base_url: URL, database_name: str) -> None:
    connection = await asyncpg.connect(_admin_dsn(base_url))
    try:
        await connection.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await connection.close()


async def _drop_database(base_url: URL, database_name: str, run_id: uuid.UUID) -> None:
    """Drop only the database this run created.

    The name is re-validated against the same guard the test runner uses and against this run's
    own identifier, so the statement below cannot address a database this process did not make.
    """
    validate_test_database_target(
        _render(base_url.set(database=database_name)),
        environ={**os.environ, "TESTING": "true", "ENVIRONMENT": "test"},
        expected_run_id=str(run_id),
    )
    if database_name != f"finspace_test_{run_id.hex}":
        raise MigrationGateError("refusing to drop a database this run did not create")
    connection = await asyncpg.connect(_admin_dsn(base_url))
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await connection.execute(f'DROP DATABASE "{database_name}"')
    finally:
        await connection.close()


async def _public_tables(dsn: str) -> set[str]:
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            "SELECT c.relname AS name FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'"
        )
        return {row["name"] for row in rows}
    finally:
        await connection.close()


async def _current_revision(dsn: str) -> str | None:
    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchval(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}")
    finally:
        await connection.close()


async def _schema_fingerprint(dsn: str) -> tuple[tuple[str, str, str], ...]:
    """Every column of every public table. Names and types only; no row is read."""
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' ORDER BY table_name, column_name"
        )
        return tuple((row["table_name"], row["column_name"], row["data_type"]) for row in rows)
    finally:
        await connection.close()


def _alembic(target: str, environment: dict[str, str], *, phase: str) -> None:
    completed = subprocess.run(
        ["alembic", "upgrade", target],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
        detail = redact(" | ".join(tail), secret=environment.get("_GATE_SECRET"))
        raise MigrationGateError(
            f"phase={phase}: alembic upgrade {target} failed "
            f"(exit {completed.returncode}): {detail}"
        )


def expected_tables() -> set[str]:
    """The application-owned tables, from SQLAlchemy metadata rather than a hand-written list."""
    return set(Base.metadata.tables)


async def _verify_schema(dsn: str, *, expected_head: str, phase: str) -> None:
    revision = await _current_revision(dsn)
    if revision != expected_head:
        raise MigrationGateError(
            f"phase={phase}: expected head {expected_head!r}, database reports {revision!r}"
        )
    present = await _public_tables(dsn) - {ALEMBIC_VERSION_TABLE}
    expected = expected_tables()
    missing = sorted(expected - present)
    if missing:
        raise MigrationGateError(
            f"phase={phase}: the migrated schema is missing {len(missing)} table(s) that "
            f"SQLAlchemy metadata declares: {missing}"
        )
    extra = sorted(present - expected)
    if extra:
        # A table the migrations create but the models do not know about is drift in the other
        # direction, and it is exactly what a fresh-install-only check would miss.
        raise MigrationGateError(
            f"phase={phase}: the migrated schema has {len(extra)} table(s) SQLAlchemy metadata "
            f"does not declare: {extra}"
        )


class _TemporaryDatabase:
    """One isolated database for one scenario, removed on success and on failure alike."""

    def __init__(self, base_url: URL) -> None:
        self.base_url = base_url
        self.run_id = uuid.uuid4()
        self.name = f"finspace_test_{self.run_id.hex}"
        self.url = _render(base_url.set(database=self.name))
        self.dsn = _render(base_url.set(drivername="postgresql", database=self.name))

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "DATABASE_URL": self.url,
            "ENVIRONMENT": "test",
            "TESTING": "true",
            "TEST_RUN_ID": str(self.run_id),
            "MIGRATION_TEST_CYCLE": "",
            "_GATE_SECRET": self.base_url.password or "",
        }

    async def __aenter__(self) -> "_TemporaryDatabase":
        await _create_database(self.base_url, self.name)
        return self

    async def __aexit__(self, *_: object) -> None:
        await _drop_database(self.base_url, self.name, self.run_id)


async def run_database_gate(
    base_url: URL,
    head: str,
    checkpoints: tuple[str, ...],
    *,
    log: object = print,
) -> list[str]:
    """Fresh install, idempotent re-run, then each historical checkpoint forward to head."""
    emit = log if callable(log) else print
    performed: list[str] = []

    async with _TemporaryDatabase(base_url) as database:
        emit(f"  fresh install        {database.name}")
        _alembic("head", database.environment(), phase="fresh-install")
        await _verify_schema(database.dsn, expected_head=head, phase="fresh-install")
        before = await _schema_fingerprint(database.dsn)

        # A second upgrade must be a no-op: same revision, byte-identical column inventory.
        _alembic("head", database.environment(), phase="idempotent-reupgrade")
        after = await _schema_fingerprint(database.dsn)
        await _verify_schema(database.dsn, expected_head=head, phase="idempotent-reupgrade")
        if before != after:
            raise MigrationGateError(
                "phase=idempotent-reupgrade: a second `alembic upgrade head` changed the schema"
            )
        performed.append("fresh-install")
        performed.append("idempotent-reupgrade")
        emit(f"    head={head} tables={len(await _public_tables(database.dsn)) - 1} idempotent=yes")

    for checkpoint in checkpoints:
        async with _TemporaryDatabase(base_url) as database:
            phase = f"checkpoint:{checkpoint}"
            emit(f"  {checkpoint:<20} {database.name}")
            _alembic(checkpoint, database.environment(), phase=phase)
            reached = await _current_revision(database.dsn)
            if reached != checkpoint:
                raise MigrationGateError(
                    f"phase={phase}: expected to stop at {checkpoint!r}, database reports "
                    f"{reached!r}"
                )
            _alembic("head", database.environment(), phase=f"{phase}->head")
            await _verify_schema(database.dsn, expected_head=head, phase=f"{phase}->head")
            performed.append(phase)
            emit(f"    {checkpoint} -> {head} ok")

    return performed


def resolve_base_url() -> URL:
    base_value = os.environ.get("TEST_DATABASE_URL", "")
    if not base_value:
        raise MigrationGateError("TEST_DATABASE_URL is required for the database gate")
    target = validate_test_database_target(
        base_value,
        environ={**os.environ, "TESTING": "true", "ENVIRONMENT": "test"},
    )
    return target.url


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the Alembic migration chain and prove a clean database reaches head.",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="validate the migration graph only; do not create a database",
    )
    parser.add_argument(
        "--expect-head",
        default=None,
        help="fail unless the single head is exactly this revision (for a release gate)",
    )
    parser.add_argument(
        "--expect-count",
        type=int,
        default=None,
        help="fail unless the chain has exactly this many revisions (for a release gate)",
    )
    parser.add_argument(
        "--script-location",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "alembic",
        help="alembic script directory (default: the repository's)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    try:
        graph = validate_graph(arguments.script_location)
        print("migration graph: PASS")
        print(f"  revisions            {graph.revision_count}")
        print(f"  root                 {graph.root}")
        print(f"  head                 {graph.head}")

        if arguments.expect_head is not None and graph.head != arguments.expect_head:
            raise MigrationGateError(
                f"expected head {arguments.expect_head!r}, the chain ends at {graph.head!r}"
            )
        if arguments.expect_count is not None and graph.revision_count != arguments.expect_count:
            raise MigrationGateError(
                f"expected {arguments.expect_count} revisions, the chain has {graph.revision_count}"
            )

        if arguments.static_only:
            print("migration gate: PASS (static only; no database was created)")
            return 0

        base_url = resolve_base_url()
        print("migration database gate:")
        performed = asyncio.run(run_database_gate(base_url, graph.head, DEFAULT_CHECKPOINTS))
        print(f"migration gate: PASS ({len(performed)} phases)")
        return 0
    except (MigrationGateError, DatabaseSafetyError) as error:
        print(f"migration gate: FAIL: {redact(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
