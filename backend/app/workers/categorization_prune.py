"""Bounded physical garbage collection for expired categorization previews.

The v0.12 logical TTL stays authoritative: an expired preview is refused on read whether or not this
worker has reclaimed its row yet. All this process does is reclaim storage, and it is deliberately a
dedicated process rather than a FastAPI background task (which would duplicate across replicas and
sit inside the request-serving failure domain) or a sync-worker responsibility (which returns early
whenever Google sync is disabled, and whose backlog would starve maintenance).

Every cycle is hard-bounded. With the default settings one cycle examines at most 50 workspaces and
issues at most one 100-row delete batch per workspace, so at most 5000 preview rows are reclaimed
before the worker sleeps again. There is no second pass and no inner retry loop.

Scheduling lives here; SQL lives in the repository and the transaction boundary in the service.
"""

import argparse
import asyncio
import logging
import signal
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import AsyncSessionFactory, engine
from app.repositories import categorization_previews as repository
from app.services import categorization_previews as service

logger = logging.getLogger("app.workers.categorization_prune")

STOP = asyncio.Event()


def _stop() -> None:
    STOP.set()


class CycleResult:
    """Counters for one bounded cycle, and the cursor the next cycle should start from."""

    __slots__ = ("next_cursor", "previews_deleted", "workspaces_examined", "workspaces_failed")

    def __init__(self) -> None:
        self.workspaces_examined = 0
        self.workspaces_failed = 0
        self.previews_deleted = 0
        self.next_cursor: uuid.UUID | None = None


async def run_cycle(cursor: uuid.UUID | None) -> CycleResult:
    """Examine at most ``max_workspaces_per_cycle`` workspaces starting after ``cursor``.

    One instant is sampled here and reused for every expiry comparison in the cycle, so workspaces
    examined late are not judged against a later clock than workspaces examined early. That is the
    right semantics for a maintenance sweep and is deliberately unlike the recurring-rule executor,
    where each sampling point represents a distinct stage of one execution.

    Enumeration failure raises: the caller decides whether that ends the process (``--once``) or is
    logged and retried next cycle (daemon). Per-workspace failures never propagate.
    """
    cycle_now = datetime.now(UTC)
    # Converted once per cycle: the service and repository take an explicit window and never read
    # settings, so the retention boundary stays deterministic under test.
    recovery_window = timedelta(seconds=settings.categorization_apply_recovery_seconds)
    result = CycleResult()
    limit = settings.categorization_prune_max_workspaces_per_cycle

    async with AsyncSessionFactory() as session:
        workspace_ids = await repository.list_workspace_ids_after(
            session,
            after_id=cursor,
            limit=limit,
        )

    interrupted = False
    for workspace_id in workspace_ids:
        if STOP.is_set():
            # Shutdown requested: the batch just committed stands, and no new workspace is started.
            # The cursor already points past the last workspace that was actually examined.
            interrupted = True
            break
        # The cursor advances past a failed workspace too, so one poison workspace cannot block
        # every workspace that sorts after it. It is retried on the next rotation.
        result.next_cursor = workspace_id
        result.workspaces_examined += 1
        try:
            async with AsyncSessionFactory() as session:
                result.previews_deleted += await service.prune_workspace_previews(
                    session,
                    workspace_id,
                    now=cycle_now,
                    recovery_window=recovery_window,
                    batch_size=settings.categorization_prune_batch_size,
                )
        except Exception as error:
            # The session context manager closes and rolls back the failed transaction for us.
            result.workspaces_failed += 1
            logger.warning(
                "categorization_prune_workspace_failed "
                f"workspace_id={workspace_id} error_type={type(error).__name__}"
            )

    if not interrupted and len(workspace_ids) < limit:
        # The page was short *and* the whole page was examined, so the table ended here: start the
        # next cycle from the beginning. Never wrap inside one cycle, and never treat an
        # interrupted cycle as the end of the table — the unexamined tail must not be skipped.
        result.next_cursor = None
    return result


async def _run_one_cycle(cursor: uuid.UUID | None) -> CycleResult:
    logger.debug(f"categorization_prune_cycle_started cursor={cursor}")
    started = datetime.now(UTC)
    result = await run_cycle(cursor)
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    logger.info(
        "categorization_prune_cycle_finished "
        f"workspaces_examined={result.workspaces_examined} "
        f"workspaces_failed={result.workspaces_failed} "
        f"previews_deleted={result.previews_deleted} "
        f"duration_ms={duration_ms} "
        f"next_cursor={result.next_cursor}"
    )
    return result


def _install_signal_handlers() -> None:
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, _stop)
        except NotImplementedError:
            signal.signal(name, lambda *_: _stop())


async def run_once() -> int:
    """Execute exactly one bounded cycle. Returns a process exit code."""
    if not settings.categorization_prune_enabled:
        logger.info("categorization_prune_disabled")
        return 0
    try:
        await _run_one_cycle(None)
    except Exception:
        # Enumeration or another cycle-wide failure. Per-workspace failures never reach here.
        logger.exception("categorization_prune_cycle_failed")
        return 1
    finally:
        await engine.dispose()
    return 0


async def run() -> None:
    if not settings.categorization_prune_enabled:
        logger.info("categorization_prune_disabled")
        return
    # One line per process, so a restart loop is obvious in `docker compose logs` and the effective
    # bounds are on the record next to the cycles they produced.
    logger.info(
        "categorization_prune_started "
        f"poll_seconds={settings.categorization_prune_poll_seconds} "
        f"batch_size={settings.categorization_prune_batch_size} "
        f"max_workspaces_per_cycle={settings.categorization_prune_max_workspaces_per_cycle} "
        f"apply_recovery_seconds={settings.categorization_apply_recovery_seconds}"
    )
    _install_signal_handlers()
    cursor: uuid.UUID | None = None
    while not STOP.is_set():
        try:
            cursor = (await _run_one_cycle(cursor)).next_cursor
        except Exception:
            # A cycle-wide failure must never end the daemon; the next cycle starts from the same
            # cursor and the situation is usually transient (database restart, network blip).
            logger.exception("categorization_prune_cycle_failed")
        if STOP.is_set():
            break
        try:
            await asyncio.wait_for(STOP.wait(), timeout=settings.categorization_prune_poll_seconds)
        except TimeoutError:
            pass
    # Reached only by STOP, so this line distinguishes a clean SIGTERM from a crash or a restart
    # loop. A cycle-wide failure never ends the daemon, so it can never reach here.
    logger.info("categorization_prune_stopping")
    await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reclaim expired categorization previews in bounded batches",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run exactly one bounded cycle and exit instead of polling",
    )
    arguments = parser.parse_args(argv)
    # Configured here and nowhere else: `run`, `run_once` and `run_cycle` stay callable from tests
    # without mutating the calling process's global logging state.
    configure_logging()
    if arguments.once:
        return asyncio.run(run_once())
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
