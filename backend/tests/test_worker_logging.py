"""Stage A: worker processes must be observable through ordinary `docker compose logs`.

Two separable concerns are covered here and deliberately kept apart:

* the shared logging configuration — one JSON handler, idempotent, with third-party request logging
  bounded so Google spreadsheet identifiers cannot appear in normal INFO output;
* the worker entrypoints — logging is configured in ``main()`` and nowhere else, so importing a
  worker or calling ``run``/``run_once`` directly (as the Stage A pruning suite does) never mutates
  the calling process's global logging state.

Every test restores the global logging state it touched, so ordering cannot affect the full suite.
None of these tests needs a database.
"""

import ast
import asyncio
import io
import json
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.core.logging import JsonFormatter, configure_logging
from app.workers import categorization_prune as prune_worker
from app.workers import sync_worker

APPLICATION_LOGGER = "app.workers.categorization_prune"
THIRD_PARTY_LOGGERS = ("httpx", "httpcore")


@pytest.fixture
def logging_state() -> Iterator[None]:
    """Snapshot and restore everything ``configure_logging`` is allowed to touch."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    root_level = root.level
    levels = {name: logging.getLogger(name).level for name in (*THIRD_PARTY_LOGGERS, "sqlalchemy")}
    disabled = root.manager.disable
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(root_level)
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
        root.manager.disable = disabled


def _console_handlers() -> list[logging.StreamHandler]:
    """The handlers this application installs.

    Identified by formatter rather than by root handler count: pytest attaches its own capture
    handlers to the root logger around every test, and those are not ours to assert on.
    """
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
        and isinstance(handler.formatter, JsonFormatter)
    ]


def _bind_console(buffer: io.StringIO) -> list[logging.StreamHandler]:
    """Point every installed console handler at ``buffer``.

    Retargeting the real handler is what makes these assertions deterministic: the configuration
    resolves ``ext://sys.stdout`` once, and pytest reassigns ``sys.stdout`` between the fixture and
    call phases, so capturing by replacing ``sys.stdout`` would silently miss records. Binding every
    matching handler also means an accumulated duplicate would show up as a duplicated line.
    """
    handlers = _console_handlers()
    for handler in handlers:
        handler.setStream(buffer)
    return handlers


@pytest.fixture
def log_stream(logging_state: None) -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    configure_logging()
    _bind_console(buffer)
    yield buffer


def _records(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_configure_logging_installs_exactly_one_console_handler_and_stays_idempotent(
    log_stream: io.StringIO,
) -> None:
    assert len(_console_handlers()) == 1

    configure_logging()
    configure_logging()

    assert len(_console_handlers()) == 1, "repeated configuration accumulated handlers"
    _bind_console(log_stream)
    logging.getLogger(APPLICATION_LOGGER).info("single_line_expected")
    assert len(_records(log_stream)) == 1, "repeated configuration duplicated log lines"


def test_application_info_is_emitted_as_json_with_the_shared_schema(
    log_stream: io.StringIO,
) -> None:
    logging.getLogger(APPLICATION_LOGGER).info("categorization_prune_cycle_finished probe=1")

    records = _records(log_stream)
    assert len(records) == 1
    record = records[0]
    assert set(record) == {"timestamp", "level", "service", "message", "request_id", "logger"}
    assert record["level"] == "INFO"
    # The worker keeps the backend schema; `logger` is what distinguishes the process.
    assert record["service"] == "backend"
    assert record["logger"] == APPLICATION_LOGGER
    assert record["message"] == "categorization_prune_cycle_finished probe=1"
    assert record["request_id"] == "-"


def test_application_warning_and_exception_survive_configuration(
    log_stream: io.StringIO,
) -> None:
    logger = logging.getLogger(APPLICATION_LOGGER)
    logger.warning("categorization_prune_workspace_failed error_type=RuntimeError")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("categorization_prune_cycle_failed")

    records = _records(log_stream)
    assert [record["level"] for record in records] == ["WARNING", "ERROR"]
    assert "exception" in records[1]
    assert "RuntimeError" in records[1]["exception"]
    assert "exception" not in records[0]


@pytest.mark.parametrize("name", THIRD_PARTY_LOGGERS)
def test_third_party_request_logging_is_bounded_to_warning(
    log_stream: io.StringIO,
    name: str,
) -> None:
    logger = logging.getLogger(name)

    logger.info("informational request line")
    assert _records(log_stream) == [], f"{name} INFO reached the log"

    logger.warning("safe transport warning")
    records = _records(log_stream)
    assert len(records) == 1
    assert records[0]["level"] == "WARNING"
    assert records[0]["message"] == "safe transport warning"


def test_google_request_urls_cannot_reach_normal_output(log_stream: io.StringIO) -> None:
    """The exact shape httpx emits per request: the URL carries the sheet id and its A1 range."""
    spreadsheet_id = f"gate-{uuid.uuid4().hex}"
    a1_range = "Sheet1!A1:Z99"
    logging.getLogger("httpx").info(
        "HTTP Request: GET https://sheets.googleapis.com/v4/spreadsheets/"
        f'{spreadsheet_id}/values/{a1_range} "HTTP/1.1 200 OK"'
    )

    output = log_stream.getvalue()
    assert spreadsheet_id not in output
    assert a1_range not in output
    assert "sheets.googleapis.com" not in output

    logging.getLogger("httpx").warning("safe transport warning")
    assert "safe transport warning" in log_stream.getvalue()


def test_sqlalchemy_does_not_emit_statements_or_parameters_under_root_info(
    log_stream: io.StringIO,
) -> None:
    """Importing SQLAlchemy pins its logger to WARNING, so root INFO cannot leak SQL.

    Proven by emission rather than by reading a level: a parameterized statement is executed
    against an in-memory database and the bound value must not appear anywhere in the output.
    """
    assert logging.getLogger("sqlalchemy.engine.Engine").getEffectiveLevel() > logging.INFO

    secret = f"bound-{uuid.uuid4().hex}"
    engine = create_engine("sqlite://", echo=False)
    try:
        with engine.connect() as connection:
            connection.execute(text("select :probe as value"), {"probe": secret}).all()
    finally:
        engine.dispose()

    output = log_stream.getvalue()
    assert secret not in output
    assert "select" not in output.lower()


def _info_messages(module_path: Path) -> list[str]:
    """Every literal template passed to ``logger.info`` in one module."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    messages: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "info" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            messages.append(argument.value)
        elif isinstance(argument, ast.JoinedStr):
            messages.append(
                "".join(
                    part.value
                    for part in argument.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
    return messages


def test_no_worker_info_template_names_a_spreadsheet_or_row_payload() -> None:
    """Enabling INFO must not start logging anything sensitive from either worker."""
    forbidden = (
        "spreadsheet",
        "sheet_id",
        "access_token",
        "refresh_token",
        "authorization",
        "amount",
        "description",
        "counterparty",
        "payee",
        "transaction_id",
        "preview_id",
        "category",
        "rule",
        "values",
        "row",
    )
    for module in (sync_worker, prune_worker):
        path = Path(module.__file__ or "")
        assert path.is_file()
        for message in _info_messages(path):
            lowered = message.lower()
            for token in forbidden:
                assert token not in lowered, f"{path.name} logs {token!r} at INFO: {message!r}"


def test_sync_worker_info_surface_is_the_audited_set() -> None:
    """A new INFO line in the sync worker must be reviewed rather than appear silently."""
    assert sorted(_info_messages(Path(sync_worker.__file__ or ""))) == [
        "Google OAuth provider is disabled or not configured; worker is idle",
        "Google Sheets sync worker is disabled",
    ]


def test_sync_worker_batch_failure_carries_its_code_in_the_message() -> None:
    """JsonFormatter drops `extra=`, so the machine-readable code must live in the text."""
    source = Path(sync_worker.__file__ or "").read_text(encoding="utf-8")
    assert "google_sync_batch_failed" in source
    assert 'extra={"code"' not in source


def test_both_workers_identify_themselves_by_an_explicit_logger_name() -> None:
    """`python -m` runs the module as ``__main__``, so the name cannot come from ``__name__``."""
    assert sync_worker.logger.name == "app.workers.sync_worker"
    assert prune_worker.logger.name == APPLICATION_LOGGER


def test_main_entrypoints_configure_logging_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    started: list[str] = []

    async def fake_sync_run() -> None:
        started.append("sync_worker.run")

    monkeypatch.setattr(sync_worker, "configure_logging", lambda: calls.append("sync"))
    monkeypatch.setattr(sync_worker, "run", fake_sync_run)
    sync_worker.main()

    assert calls == ["sync"]
    assert started == ["sync_worker.run"]


def test_prune_once_entrypoint_configures_logging_and_keeps_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    selected: list[str] = []

    async def fake_run_once() -> int:
        selected.append("run_once")
        return 1

    monkeypatch.setattr(prune_worker, "configure_logging", lambda: calls.append("prune"))
    monkeypatch.setattr(prune_worker, "run_once", fake_run_once)

    assert prune_worker.main(["--once"]) == 1
    assert calls == ["prune"]
    assert selected == ["run_once"]


def test_prune_daemon_entrypoint_configures_logging_and_selects_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    selected: list[str] = []

    async def fake_run() -> None:
        selected.append("run")

    monkeypatch.setattr(prune_worker, "configure_logging", lambda: calls.append("prune"))
    monkeypatch.setattr(prune_worker, "run", fake_run)

    assert prune_worker.main([]) == 0
    assert calls == ["prune"]
    assert selected == ["run"]


def test_direct_run_once_and_run_never_configure_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v0.13 test model: calling the coroutines directly must not touch global logging.

    Both workers are switched off so neither reaches a database, which is the point — the assertion
    is about the logging side effect, not about the cycle body.
    """
    calls: list[str] = []
    monkeypatch.setattr(prune_worker, "configure_logging", lambda: calls.append("prune"))
    monkeypatch.setattr(sync_worker, "configure_logging", lambda: calls.append("sync"))
    monkeypatch.setattr(settings, "categorization_prune_enabled", False)
    monkeypatch.setattr(settings, "google_sheets_sync_enabled", False)

    assert asyncio.run(prune_worker.run_once()) == 0
    asyncio.run(prune_worker.run())
    asyncio.run(sync_worker.run())

    assert calls == []


class _StubEngine:
    """Stands in for the shared engine so a worker exit cannot close the suite's pool."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


@pytest.fixture
def prune_records(logging_state: None) -> Iterator[list[logging.LogRecord]]:
    """Capture the prune worker's own records without configuring global logging."""
    logger = logging.getLogger(APPLICATION_LOGGER)
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.DEBUG)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    previous_stop = prune_worker.STOP
    prune_worker.STOP = asyncio.Event()
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        prune_worker.STOP = previous_stop


def _messages(records: list[logging.LogRecord], level: int | None = None) -> list[str]:
    return [record.getMessage() for record in records if level is None or record.levelno == level]


def test_daemon_logs_one_start_line_with_effective_bounds_and_one_stop_line(
    prune_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "categorization_prune_enabled", True)
    monkeypatch.setattr(settings, "categorization_prune_poll_seconds", 900)
    monkeypatch.setattr(settings, "categorization_prune_batch_size", 100)
    monkeypatch.setattr(settings, "categorization_prune_max_workspaces_per_cycle", 50)
    monkeypatch.setattr(prune_worker, "engine", _StubEngine())

    async def one_cycle(cursor: uuid.UUID | None) -> prune_worker.CycleResult:
        assert cursor is None
        prune_worker.STOP.set()
        return prune_worker.CycleResult()

    monkeypatch.setattr(prune_worker, "run_cycle", one_cycle)
    asyncio.run(prune_worker.run())

    info = _messages(prune_records, logging.INFO)
    assert len(info) == 3, info
    assert info[0] == (
        "categorization_prune_started poll_seconds=900 batch_size=100 max_workspaces_per_cycle=50"
    )
    assert info[1].startswith("categorization_prune_cycle_finished ")
    for field in (
        "workspaces_examined=0",
        "workspaces_failed=0",
        "previews_deleted=0",
        "duration_ms=",
        "next_cursor=None",
    ):
        assert field in info[1]
    assert info[2] == "categorization_prune_stopping"
    # One cycle over any number of workspaces must never add per-workspace success lines.
    assert _messages(prune_records, logging.WARNING) == []


def test_disabled_daemon_logs_only_the_disabled_line(
    prune_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "categorization_prune_enabled", False)
    asyncio.run(prune_worker.run())

    assert _messages(prune_records, logging.INFO) == ["categorization_prune_disabled"]


def test_a_fatal_cycle_logs_an_error_backs_off_and_still_stops_cleanly(
    prune_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "categorization_prune_enabled", True)
    # Small enough that the back-off is observable inside a test, still the real sleep path.
    monkeypatch.setattr(settings, "categorization_prune_poll_seconds", 0.01)
    monkeypatch.setattr(prune_worker, "engine", _StubEngine())
    attempts: list[int] = []

    async def failing_then_stopping(_cursor: uuid.UUID | None) -> prune_worker.CycleResult:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("simulated enumeration failure")
        prune_worker.STOP.set()
        return prune_worker.CycleResult()

    monkeypatch.setattr(prune_worker, "run_cycle", failing_then_stopping)
    asyncio.run(prune_worker.run())

    errors = [record for record in prune_records if record.levelno == logging.ERROR]
    assert [record.getMessage() for record in errors] == ["categorization_prune_cycle_failed"]
    assert errors[0].exc_info is not None
    # The daemon survived the failure, slept, ran again and then stopped cleanly.
    assert len(attempts) == 2
    assert _messages(prune_records, logging.INFO)[-1] == "categorization_prune_stopping"
