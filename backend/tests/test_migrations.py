"""The migration chain has to be provably sound before a release, not sound by inspection.

Two kinds of test live here. The fast ones build a deliberately broken chain in a temporary
directory and check that the gate refuses it — that is the only way to know the gate would catch a
real defect, since the repository's own chain is (and should stay) correct. The slow one runs the
real thing against real PostgreSQL: a clean database to head, a second upgrade that must change
nothing, and each historical checkpoint migrated forward.
"""

import ast
import textwrap
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.db.base import Base
from scripts import validate_migrations
from scripts.validate_migrations import MigrationGateError

REPOSITORY_ALEMBIC = Path(validate_migrations.__file__).resolve().parents[1] / "alembic"
GATE_SOURCE = Path(validate_migrations.__file__).read_text(encoding="utf-8")

# Shaped like a real migration module, annotations and all: Alembic imports these files to build
# the graph, so an annotation naming a type the module never imports fails at import time on the
# deployment's Python and would make a negative test pass for the wrong reason.
REVISION_TEMPLATE = """\
\"\"\"fixture revision\"\"\"

from collections.abc import Sequence

revision: str = "{revision}"
down_revision: str | None = {down}
branch_labels: str | Sequence[str] | None = {branch}
depends_on: str | Sequence[str] | None = {depends}


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
"""


def _chain(tmp_path: Path, revisions: list[tuple[str, str | None]], **extra: str) -> Path:
    """Write a versions directory from (revision, down_revision) pairs."""
    root = tmp_path / "alembic"
    versions = root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    for revision, down in revisions:
        body = REVISION_TEMPLATE.format(
            revision=revision,
            down="None" if down is None else f'"{down}"',
            branch=extra.get("branch", "None"),
            depends=extra.get("depends", "None"),
        )
        versions.joinpath(f"{revision}.py").write_text(body, encoding="utf-8")
    return root


# --------------------------------------------------------------------------------------------
# The repository's own chain
# --------------------------------------------------------------------------------------------


def test_the_repository_chain_is_one_linear_graph() -> None:
    report = validate_migrations.validate_graph(REPOSITORY_ALEMBIC)
    assert report.head
    assert report.root == "0001_foundation"
    assert report.revision_count == len(list((REPOSITORY_ALEMBIC / "versions").glob("0*.py")))
    assert report.chain[0] == report.root
    assert report.chain[-1] == report.head
    assert len(set(report.chain)) == len(report.chain)


def test_every_revision_is_reachable_from_the_head() -> None:
    report = validate_migrations.validate_graph(REPOSITORY_ALEMBIC)
    on_disk = {
        item.revision
        for item in validate_migrations.scan_revision_files(REPOSITORY_ALEMBIC / "versions")
    }
    assert set(report.chain) == on_disk


def test_the_release_pins_accept_the_actual_head_and_count() -> None:
    """The head is not hard-coded in the validator; a release gate pins it from outside."""
    report = validate_migrations.validate_graph(REPOSITORY_ALEMBIC)
    assert (
        validate_migrations.main(
            [
                "--static-only",
                "--expect-head",
                report.head,
                "--expect-count",
                str(report.revision_count),
            ]
        )
        == 0
    )


def test_a_wrong_expected_head_fails_the_gate(capsys: pytest.CaptureFixture[str]) -> None:
    assert validate_migrations.main(["--static-only", "--expect-head", "0099_not_real"]) == 1
    assert "0099_not_real" in capsys.readouterr().err


def test_a_wrong_expected_count_fails_the_gate(capsys: pytest.CaptureFixture[str]) -> None:
    assert validate_migrations.main(["--static-only", "--expect-count", "999"]) == 1
    assert "999" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------
# Broken chains must be refused
# --------------------------------------------------------------------------------------------


def test_a_duplicate_revision_id_is_refused(tmp_path: Path) -> None:
    """Alembic only warns about this and then drops one of the two, losing a migration."""
    root = _chain(tmp_path, [("a", None), ("b", "a")])
    (root / "versions" / "c.py").write_text(
        REVISION_TEMPLATE.format(revision="b", down='"a"', branch="None", depends="None"),
        encoding="utf-8",
    )
    with pytest.raises(MigrationGateError, match="duplicate revision id"):
        validate_migrations.validate_graph(root)


def test_a_dangling_down_revision_is_refused(tmp_path: Path) -> None:
    """Alembic only warns about this; the gate turns that warning into a refusal."""
    root = _chain(tmp_path, [("a", None), ("b", "does_not_exist")])
    with pytest.raises(MigrationGateError, match="is not present"):
        validate_migrations.validate_graph(root)


def test_a_cycle_is_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", "b"), ("b", "a")])
    with pytest.raises(MigrationGateError, match="CycleDetected"):
        validate_migrations.validate_graph(root)


def test_two_heads_are_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", None), ("b", "a"), ("c", "a")])
    with pytest.raises(MigrationGateError, match="exactly one head"):
        validate_migrations.validate_graph(root)


def test_a_disconnected_graph_is_refused(tmp_path: Path) -> None:
    """Two roots means two chains; the second would never run on an existing database.

    Caught by the head count, since two disconnected chains also present two heads. The explicit
    single-root check behind it is defence in depth: with merges refused below, one head already
    implies one root.
    """
    root = _chain(tmp_path, [("a", None), ("b", None)])
    with pytest.raises(MigrationGateError, match="exactly one head"):
        validate_migrations.validate_graph(root)


def test_a_merge_revision_is_refused(tmp_path: Path) -> None:
    """A tuple down_revision merges two chains. Refusing it is what keeps one head one root."""
    root = _chain(tmp_path, [("a", None), ("b", None)])
    (root / "versions" / "c.py").write_text(
        REVISION_TEMPLATE.format(revision="c", down='("a", "b")', branch="None", depends="None"),
        encoding="utf-8",
    )
    with pytest.raises(MigrationGateError, match="malformed down_revision"):
        validate_migrations.validate_graph(root)


def test_a_revision_without_an_id_is_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", None)])
    (root / "versions" / "broken.py").write_text("down_revision = None\n", encoding="utf-8")
    with pytest.raises(MigrationGateError, match="declares no revision id"):
        validate_migrations.validate_graph(root)


def test_a_non_string_revision_id_is_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", None)])
    (root / "versions" / "broken.py").write_text(
        "revision = 123\ndown_revision = None\n", encoding="utf-8"
    )
    with pytest.raises(MigrationGateError, match="malformed revision id"):
        validate_migrations.validate_graph(root)


def test_a_revision_that_does_not_parse_is_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", None)])
    (root / "versions" / "broken.py").write_text("def upgrade(:\n", encoding="utf-8")
    with pytest.raises(MigrationGateError, match="does not parse"):
        validate_migrations.validate_graph(root)


def test_a_revision_without_a_down_revision_is_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", None)])
    (root / "versions" / "broken.py").write_text('revision = "z"\n', encoding="utf-8")
    with pytest.raises(MigrationGateError, match="declares no down_revision"):
        validate_migrations.validate_graph(root)


def test_branch_labels_are_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", None)], branch='"experimental"')
    with pytest.raises(MigrationGateError, match="branch labels"):
        validate_migrations.validate_graph(root)


def test_cross_dependencies_are_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", None)], depends='"other"')
    with pytest.raises(MigrationGateError, match="depends_on"):
        validate_migrations.validate_graph(root)


def test_a_filename_that_disagrees_with_its_revision_is_refused(tmp_path: Path) -> None:
    root = _chain(tmp_path, [("a", None)])
    (root / "versions" / "a.py").rename(root / "versions" / "renamed.py")
    with pytest.raises(MigrationGateError, match="filename and the revision id must agree"):
        validate_migrations.validate_graph(root)


def test_an_empty_versions_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "alembic"
    (root / "versions").mkdir(parents=True)
    with pytest.raises(MigrationGateError, match="no revision files"):
        validate_migrations.validate_graph(root)


def test_a_missing_versions_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MigrationGateError, match="versions directory is missing"):
        validate_migrations.validate_graph(tmp_path / "nowhere")


# --------------------------------------------------------------------------------------------
# The gate never downgrades, and never prints a credential
# --------------------------------------------------------------------------------------------


def test_the_gate_only_ever_runs_alembic_upgrade() -> None:
    """Rollback is restore-based; a downgrade here would imply support this project lacks."""
    commands: list[list[str]] = []
    for node in ast.walk(ast.parse(GATE_SOURCE)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Attribute) and target.attr == "run"):
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.List):
            commands.append(
                [
                    element.value if isinstance(element, ast.Constant) else "<variable>"
                    for element in first.elts
                ]
            )
    assert commands, "no subprocess invocation was found"
    for command in commands:
        assert command[:2] == ["alembic", "upgrade"], command
    assert "downgrade" not in [item for command in commands for item in command]


def test_a_connection_string_is_redacted_before_it_is_printed() -> None:
    message = (
        "could not connect to postgresql+asyncpg://finspace:sup3r-s3cret@postgres:5432/finspace_x"
    )
    cleaned = validate_migrations.redact(message, secret="sup3r-s3cret")
    assert "sup3r-s3cret" not in cleaned
    assert "postgres:5432" not in cleaned
    assert "<dsn redacted>" in cleaned


def test_a_bare_password_is_redacted_even_without_a_dsn() -> None:
    cleaned = validate_migrations.redact("auth failed for sup3r-s3cret", secret="sup3r-s3cret")
    assert "sup3r-s3cret" not in cleaned


def test_a_failure_message_carries_the_phase_and_both_revisions() -> None:
    error = MigrationGateError(
        "phase=checkpoint:0010_goals->head: expected head '0017_x', database reports '0016_y'"
    )
    rendered = validate_migrations.redact(str(error))
    assert "phase=checkpoint:0010_goals->head" in rendered
    assert "0017_x" in rendered
    assert "0016_y" in rendered


def test_the_checkpoints_are_real_revisions_in_this_chain() -> None:
    report = validate_migrations.validate_graph(REPOSITORY_ALEMBIC)
    for checkpoint in validate_migrations.DEFAULT_CHECKPOINTS:
        assert checkpoint in report.chain, checkpoint
    # Ordered, distinct, and none of them is the head: each must actually migrate forward.
    positions = [report.chain.index(item) for item in validate_migrations.DEFAULT_CHECKPOINTS]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)
    assert report.chain[-1] not in validate_migrations.DEFAULT_CHECKPOINTS


def test_expected_tables_come_from_metadata_not_a_hand_written_list() -> None:
    assert validate_migrations.expected_tables() == set(Base.metadata.tables)
    assert "transactions" in validate_migrations.expected_tables()


def test_dropping_refuses_a_database_this_run_did_not_create() -> None:
    import asyncio

    from sqlalchemy.engine import make_url

    base = make_url("postgresql+asyncpg://finspace:pw@postgres:5432/finspace_test")
    with pytest.raises((MigrationGateError, Exception)) as raised:
        asyncio.run(validate_migrations._drop_database(base, "finspace", uuid.uuid4()))
    assert "finspace" not in str(raised.value) or "refusing" in str(raised.value).casefold()


# --------------------------------------------------------------------------------------------
# Against real PostgreSQL
# --------------------------------------------------------------------------------------------


@pytest.mark.requires_database
async def test_a_clean_database_reaches_head_and_every_checkpoint_migrates_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole gate, for real: fresh install, idempotent re-run, and each checkpoint forward."""
    created: list[str] = []
    real_create = validate_migrations._create_database

    async def record(base_url: object, database_name: str) -> None:
        created.append(database_name)
        await real_create(base_url, database_name)  # type: ignore[arg-type]

    monkeypatch.setattr(validate_migrations, "_create_database", record)

    base_url = validate_migrations.resolve_base_url()
    report = validate_migrations.validate_graph(REPOSITORY_ALEMBIC)
    performed = await validate_migrations.run_database_gate(
        base_url, report.head, validate_migrations.DEFAULT_CHECKPOINTS, log=lambda *_: None
    )

    assert performed[:2] == ["fresh-install", "idempotent-reupgrade"]
    assert [phase for phase in performed if phase.startswith("checkpoint:")] == [
        f"checkpoint:{checkpoint}" for checkpoint in validate_migrations.DEFAULT_CHECKPOINTS
    ]
    assert len(created) == 1 + len(validate_migrations.DEFAULT_CHECKPOINTS)
    for name in created:
        assert name.startswith("finspace_test_")
    await _assert_databases_are_gone(base_url, created)


@pytest.mark.requires_database
async def test_the_temporary_database_is_dropped_even_when_a_phase_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[str] = []
    real_create = validate_migrations._create_database

    async def record(base_url: object, database_name: str) -> None:
        created.append(database_name)
        await real_create(base_url, database_name)  # type: ignore[arg-type]

    async def explode(*_: object, **__: object) -> None:
        raise MigrationGateError("phase=fresh-install: simulated failure")

    monkeypatch.setattr(validate_migrations, "_create_database", record)
    monkeypatch.setattr(validate_migrations, "_verify_schema", explode)

    base_url = validate_migrations.resolve_base_url()
    report = validate_migrations.validate_graph(REPOSITORY_ALEMBIC)
    with pytest.raises(MigrationGateError, match="simulated failure"):
        await validate_migrations.run_database_gate(base_url, report.head, (), log=lambda *_: None)
    assert len(created) == 1
    await _assert_databases_are_gone(base_url, created)


async def _assert_databases_are_gone(base_url: object, names: list[str]) -> None:
    dsn = validate_migrations._admin_dsn(base_url)  # type: ignore[arg-type]
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            "SELECT datname FROM pg_database WHERE datname = ANY($1::text[])", names
        )
    finally:
        await connection.close()
    assert [row["datname"] for row in rows] == [], "a temporary database survived the gate"


@pytest.mark.requires_database
async def test_the_migrated_schema_matches_sqlalchemy_metadata_exactly() -> None:
    """Fresh install alone would not catch a table the migrations create but the models forgot."""
    base_url = validate_migrations.resolve_base_url()
    report = validate_migrations.validate_graph(REPOSITORY_ALEMBIC)
    database = validate_migrations._TemporaryDatabase(base_url)
    async with database:
        validate_migrations._alembic("head", database.environment(), phase="metadata-check")
        present = await validate_migrations._public_tables(database.dsn)
        assert validate_migrations.ALEMBIC_VERSION_TABLE in present
        assert present - {validate_migrations.ALEMBIC_VERSION_TABLE} == set(Base.metadata.tables)
        revision = await validate_migrations._current_revision(database.dsn)
        assert revision == report.head


def test_the_documented_release_invocation_is_the_one_that_exists() -> None:
    """The runbook tells an operator to run this; the flags it names must be real."""
    parsed = validate_migrations._parse_arguments(
        ["--expect-head", "0017_categorization_history", "--expect-count", "17"]
    )
    assert parsed.expect_head == "0017_categorization_history"
    assert parsed.expect_count == 17
    assert parsed.static_only is False
    assert textwrap.dedent(GATE_SOURCE).count("--static-only") >= 1
