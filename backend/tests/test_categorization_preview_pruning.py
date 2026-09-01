"""Stage A: bounded physical pruning of expired categorization previews.

Lifecycle under test, and the contract it deliberately accepts:

* while the row exists and ``expires_at <= now`` the API answers 410
  ``CATEGORIZATION_PREVIEW_EXPIRED`` — the v0.12 logical TTL, which stays authoritative whether or
  not the worker has run;
* once the row has been physically reclaimed the same request answers 404
  ``CATEGORIZATION_PREVIEW_NOT_FOUND``, indistinguishable from a foreign-workspace preview.

Both are terminal refusals and neither can mutate anything, so the 410 -> 404 transition is an
accepted consequence of garbage collection rather than a behaviour change.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.categorization_apply_operations import (
    CategorizationApplyOperation,
    CategorizationApplyResult,
)
from app.db.models.categorization_previews import CategorizationPreview, CategorizationPreviewItem
from app.db.models.categorization_rules import CategorizationRule
from app.db.models.google_sync import SyncOutbox
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace
from app.db.session import AsyncSessionFactory
from app.dependencies.context import RequestContext
from app.repositories import categorization_previews as preview_repository
from app.services import categorization_previews as preview_service
from app.workers import categorization_prune as worker

PASSWORD = "correct horse battery staple"
PREVIEWS = "/api/v1/categorization-previews"


@pytest.fixture(autouse=True)
def _configure_pruning_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)
    monkeypatch.setattr(settings, "categorization_prune_enabled", True)


@pytest.fixture(autouse=True)
def _reset_stop() -> None:
    worker.STOP.clear()
    yield
    worker.STOP.clear()


def _register(client: TestClient, label: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"prune-{uuid.uuid4()}@example.com",
            "display_name": f"{label} Owner",
            "password": PASSWORD,
            "workspace_name": f"{label} Workspace",
            "base_currency": "RUB",
            "timezone": "Asia/Yekaterinburg",
        },
    )
    assert response.status_code == 201, response.text
    identity = response.json()
    return identity, {
        "Authorization": f"Bearer {identity['access_token']}",
        "X-Workspace-ID": identity["workspace"]["id"],
    }


def _account(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": name,
            "account_type": "debit_card",
            "currency": "RUB",
            "opening_balance": "100000",
            "opening_balance_at": "2026-07-01T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _category(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": name, "category_type": "expense"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _rule(client: TestClient, headers: dict[str, str], *, name: str, category_id: str) -> dict:
    response = client.post(
        "/api/v1/categorization-rules",
        headers=headers,
        json={
            "name": name,
            "priority": 10,
            "category_id": category_id,
            "counterparty_contains": "prune shop",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_transaction(identity: dict, account_id: str, counterparty: str) -> str:
    async with AsyncSessionFactory() as session:
        transaction = FinancialTransaction(
            workspace_id=uuid.UUID(identity["workspace"]["id"]),
            occurred_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            transaction_type="expense",
            amount=Decimal("1250.2500"),
            currency="RUB",
            account_id=uuid.UUID(account_id),
            counterparty=counterparty,
            status="confirmed",
            source="import",
            created_by=uuid.UUID(identity["user"]["id"]),
            updated_by=uuid.UUID(identity["user"]["id"]),
        )
        session.add(transaction)
        await session.commit()
        await session.refresh(transaction)
        return str(transaction.id)


def _preview(client: TestClient, headers: dict[str, str], transaction_ids: list[str]) -> dict:
    response = client.post(
        PREVIEWS,
        headers=headers,
        json={"selection": {"mode": "ids", "transaction_ids": transaction_ids}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _items(client: TestClient, headers: dict[str, str], preview_id: str) -> list[dict]:
    response = client.get(f"{PREVIEWS}/{preview_id}/items?limit=200", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _expire(preview_id: str, *, age_hours: int = 24) -> None:
    async with AsyncSessionFactory() as session:
        await session.execute(
            update(CategorizationPreview)
            .where(CategorizationPreview.id == uuid.UUID(preview_id))
            .values(
                created_at=datetime.now(UTC) - timedelta(hours=age_hours + 24),
                expires_at=datetime.now(UTC) - timedelta(hours=age_hours),
            )
        )
        await session.commit()


async def _preview_exists(preview_id: str) -> bool:
    async with AsyncSessionFactory() as session:
        return (await session.get(CategorizationPreview, uuid.UUID(preview_id))) is not None


async def _item_count(preview_id: str) -> int:
    async with AsyncSessionFactory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(CategorizationPreviewItem)
                .where(CategorizationPreviewItem.preview_id == uuid.UUID(preview_id))
            )
            or 0
        )


async def _context_for(identity: dict, session) -> RequestContext:
    user = await session.get(User, uuid.UUID(identity["user"]["id"]))
    workspace = await session.get(Workspace, uuid.UUID(identity["workspace"]["id"]))
    assert user is not None
    assert workspace is not None
    return RequestContext(
        user=user,
        workspace=workspace,
        role="owner",
        request_id=str(uuid.uuid4()),
    )


class _StubEngine:
    """Stands in for the shared engine so a worker exit cannot close the suite's pool."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def _prune(identity: dict, *, batch_size: int = 100) -> int:
    async def run() -> int:
        async with AsyncSessionFactory() as session:
            return await preview_service.prune_workspace_previews(
                session,
                uuid.UUID(identity["workspace"]["id"]),
                now=datetime.now(UTC),
                batch_size=batch_size,
            )

    return asyncio.run(run())


def test_expired_preview_is_deleted_with_its_items_and_others_are_retained(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Prune basic")
    account = _account(client, headers, "Prune card")
    category = _category(client, headers, "Prune target")
    _rule(client, headers, name="Prune rule", category_id=category["id"])
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Prune Shop one"))

    expired = _preview(client, headers, [transaction_id])
    fresh = _preview(client, headers, [transaction_id])
    assert len(_items(client, headers, expired["id"])) == 1
    asyncio.run(_expire(expired["id"]))

    # Logical TTL first: the row still exists, so the API reports 410.
    still_present = client.get(f"{PREVIEWS}/{expired['id']}", headers=headers)
    assert still_present.status_code == 410
    assert still_present.json()["error"]["code"] == "CATEGORIZATION_PREVIEW_EXPIRED"

    assert _prune(identity) == 1
    assert asyncio.run(_preview_exists(expired["id"])) is False
    assert asyncio.run(_item_count(expired["id"])) == 0
    # Cascade reached the items and stopped there; the unexpired preview is untouched.
    assert asyncio.run(_preview_exists(fresh["id"])) is True
    assert asyncio.run(_item_count(fresh["id"])) == 1

    # And now the documented 410 -> 404 transition.
    gone = client.get(f"{PREVIEWS}/{expired['id']}", headers=headers)
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "CATEGORIZATION_PREVIEW_NOT_FOUND"


def test_batch_size_caps_one_call_and_continues_oldest_first(client: TestClient) -> None:
    identity, headers = _register(client, "Prune batch")
    account = _account(client, headers, "Batch card")
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Batch Shop"))

    previews = [_preview(client, headers, [transaction_id])["id"] for _ in range(3)]
    # Distinct expiry ages so "oldest first" is observable rather than incidental.
    for index, preview_id in enumerate(previews):
        asyncio.run(_expire(preview_id, age_hours=30 - index))

    assert _prune(identity, batch_size=2) == 2
    survivors = [pid for pid in previews if asyncio.run(_preview_exists(pid))]
    # ORDER BY expires_at, id removes the two oldest, leaving the newest expiry.
    assert survivors == [previews[2]]

    assert _prune(identity, batch_size=2) == 1
    assert [pid for pid in previews if asyncio.run(_preview_exists(pid))] == []
    # Repeated cycles are idempotent.
    assert _prune(identity, batch_size=2) == 0


def test_preview_locked_by_an_apply_claim_is_skipped_promptly(client: TestClient) -> None:
    """A concurrent FOR SHARE claim must make pruning skip, never block."""
    identity, headers = _register(client, "Prune locked")
    account = _account(client, headers, "Locked card")
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Locked Shop"))
    preview = _preview(client, headers, [transaction_id])
    asyncio.run(_expire(preview["id"]))

    async def race() -> int:
        async with AsyncSessionFactory() as holder:
            # Exactly the lock a new apply claim takes while inserting its operation row.
            locked = await preview_repository.get_preview(
                holder,
                uuid.UUID(identity["workspace"]["id"]),
                uuid.UUID(preview["id"]),
                for_share=True,
            )
            assert locked is not None
            async with AsyncSessionFactory() as pruner:
                deleted = await asyncio.wait_for(
                    preview_service.prune_workspace_previews(
                        pruner,
                        uuid.UUID(identity["workspace"]["id"]),
                        now=datetime.now(UTC),
                        batch_size=100,
                    ),
                    # SKIP LOCKED means this must return immediately, not wait for the holder.
                    timeout=10,
                )
            await holder.rollback()
            return deleted

    assert asyncio.run(race()) == 0
    assert asyncio.run(_preview_exists(preview["id"])) is True
    # Once the claim is gone the same preview is reclaimed normally.
    assert _prune(identity) == 1


def test_in_progress_operation_protects_its_preview_and_completion_releases_it(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Prune operation")
    account = _account(client, headers, "Operation card")
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Operation Shop"))
    preview = _preview(client, headers, [transaction_id])
    asyncio.run(_expire(preview["id"]))

    async def add_operation(status: str) -> uuid.UUID:
        async with AsyncSessionFactory() as session:
            operation = CategorizationApplyOperation(
                workspace_id=uuid.UUID(identity["workspace"]["id"]),
                preview_id=uuid.UUID(preview["id"]),
                actor_user_id=uuid.UUID(identity["user"]["id"]),
                idempotency_key=f"prune-{uuid.uuid4()}",
                request_hash="0" * 64,
                status=status,
                requested_count=1,
            )
            session.add(operation)
            await session.commit()
            return operation.id

    operation_id = asyncio.run(add_operation("in_progress"))
    # A committed in_progress operation is the window the FOR SHARE lock cannot cover.
    assert _prune(identity) == 0
    assert asyncio.run(_preview_exists(preview["id"])) is True

    async def complete() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(CategorizationApplyOperation)
                .where(CategorizationApplyOperation.id == operation_id)
                .values(status="completed", completed_at=datetime.now(UTC))
            )
            await session.commit()

    asyncio.run(complete())
    # Completed operations carry their own replayable results, so they do not pin the preview.
    assert _prune(identity) == 1
    assert asyncio.run(_preview_exists(preview["id"])) is False


def test_completed_apply_replays_after_its_preview_is_pruned(client: TestClient) -> None:
    identity, headers = _register(client, "Prune replay")
    account = _account(client, headers, "Replay card")
    category = _category(client, headers, "Replay target")
    _rule(client, headers, name="Replay rule", category_id=category["id"])
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Prune Shop replay"))
    preview = _preview(client, headers, [transaction_id])
    item_id = _items(client, headers, preview["id"])[0]["id"]

    key = f"replay-{uuid.uuid4()}"
    first = client.post(
        f"{PREVIEWS}/{preview['id']}/apply",
        headers={**headers, "X-Idempotency-Key": key},
        json={"item_ids": [item_id]},
    )
    assert first.status_code == 200, first.text
    assert first.json()["results"][0]["status"] == "applied"

    async def counts() -> tuple[int, int, int, int]:
        async with AsyncSessionFactory() as session:
            audits = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.entity_type == "transaction",
                        AuditLog.entity_id == uuid.UUID(transaction_id),
                        AuditLog.action == "update",
                    )
                )
                or 0
            )
            outbox = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncOutbox)
                    .where(SyncOutbox.entity_id == uuid.UUID(transaction_id))
                )
                or 0
            )
            results = int(
                await session.scalar(select(func.count()).select_from(CategorizationApplyResult))
                or 0
            )
            transaction = await session.get(FinancialTransaction, uuid.UUID(transaction_id))
            assert transaction is not None
            return audits, outbox, results, transaction.version

    before = asyncio.run(counts())
    asyncio.run(_expire(preview["id"]))
    assert _prune(identity) == 1
    assert asyncio.run(_preview_exists(preview["id"])) is False
    assert asyncio.run(_item_count(preview["id"])) == 0

    # Same key, same logical request: the persisted terminal results answer without the preview.
    replay = client.post(
        f"{PREVIEWS}/{preview['id']}/apply",
        headers={**headers, "X-Idempotency-Key": key},
        json={"item_ids": [item_id]},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert asyncio.run(counts()) == before
    assert asyncio.run(_preview_exists(preview["id"])) is False

    # A brand new key against the pruned preview is a plain 404 and creates nothing.
    async def operation_count() -> int:
        async with AsyncSessionFactory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(CategorizationApplyOperation)
                    .where(
                        CategorizationApplyOperation.workspace_id
                        == uuid.UUID(identity["workspace"]["id"])
                    )
                )
                or 0
            )

    operations_before = asyncio.run(operation_count())
    fresh = client.post(
        f"{PREVIEWS}/{preview['id']}/apply",
        headers={**headers, "X-Idempotency-Key": f"fresh-{uuid.uuid4()}"},
        json={"item_ids": [item_id]},
    )
    assert fresh.status_code == 404
    assert fresh.json()["error"]["code"] == "CATEGORIZATION_PREVIEW_NOT_FOUND"
    assert asyncio.run(operation_count()) == operations_before
    assert asyncio.run(_preview_exists(preview["id"])) is False


def test_pruning_mutates_no_financial_or_categorization_state(client: TestClient) -> None:
    identity, headers = _register(client, "Prune inert")
    account = _account(client, headers, "Inert card")
    category = _category(client, headers, "Inert target")
    rule = _rule(client, headers, name="Inert rule", category_id=category["id"])
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Prune Shop inert"))
    preview = _preview(client, headers, [transaction_id])
    asyncio.run(_expire(preview["id"]))

    async def snapshot() -> tuple:
        async with AsyncSessionFactory() as session:
            transaction = await session.get(FinancialTransaction, uuid.UUID(transaction_id))
            rule_row = await session.get(CategorizationRule, uuid.UUID(rule["id"]))
            assert transaction is not None and rule_row is not None
            audits = int(await session.scalar(select(func.count()).select_from(AuditLog)) or 0)
            outbox = int(await session.scalar(select(func.count()).select_from(SyncOutbox)) or 0)
            return (
                transaction.version,
                transaction.category_id,
                transaction.status,
                rule_row.version,
                rule_row.is_active,
                audits,
                outbox,
            )

    before = asyncio.run(snapshot())
    assert _prune(identity) == 1
    assert asyncio.run(snapshot()) == before


def test_cycle_is_bounded_by_max_workspaces_and_gives_one_batch_per_workspace(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with nothing to delete anywhere, a cycle examines at most the configured workspaces."""
    identity, headers = _register(client, "Prune bound")
    account = _account(client, headers, "Bound card")
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Bound Shop"))
    # Two expired previews in one workspace: one cycle must take exactly one batch of one.
    first = _preview(client, headers, [transaction_id])
    second = _preview(client, headers, [transaction_id])
    asyncio.run(_expire(first["id"], age_hours=30))
    asyncio.run(_expire(second["id"], age_hours=29))

    monkeypatch.setattr(settings, "categorization_prune_max_workspaces_per_cycle", 2)
    monkeypatch.setattr(settings, "categorization_prune_batch_size", 1)

    calls: list[uuid.UUID] = []
    original = worker.service.prune_workspace_previews

    async def counting(session, workspace_id, *, now, batch_size):
        calls.append(workspace_id)
        return await original(session, workspace_id, now=now, batch_size=batch_size)

    monkeypatch.setattr(worker.service, "prune_workspace_previews", counting)

    result = asyncio.run(worker.run_cycle(None))
    assert result.workspaces_examined <= 2
    assert result.workspaces_failed == 0
    # One batch per workspace per cycle, never two.
    assert len(calls) == len(set(calls)) == result.workspaces_examined
    workspace_id = uuid.UUID(identity["workspace"]["id"])
    if workspace_id in calls:
        assert result.previews_deleted <= 2
        assert asyncio.run(_preview_exists(first["id"])) is False
        assert asyncio.run(_preview_exists(second["id"])) is True


def test_cursor_advances_and_resets_at_the_end_of_the_table(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        _register(client, f"Prune rotation {index}")

    async def all_ids() -> list[uuid.UUID]:
        async with AsyncSessionFactory() as session:
            return await preview_repository.list_workspace_ids_after(
                session, after_id=None, limit=1000
            )

    ordered = asyncio.run(all_ids())
    assert len(ordered) >= 3

    monkeypatch.setattr(settings, "categorization_prune_max_workspaces_per_cycle", 1)
    first = asyncio.run(worker.run_cycle(None))
    assert first.workspaces_examined == 1
    assert first.next_cursor == ordered[0]

    second = asyncio.run(worker.run_cycle(first.next_cursor))
    assert second.workspaces_examined == 1
    # Continues from the last examined workspace rather than restarting.
    assert second.next_cursor == ordered[1]

    # A short page means the table ended: the next cycle starts over, without wrapping in this one.
    monkeypatch.setattr(settings, "categorization_prune_max_workspaces_per_cycle", 500)
    final = asyncio.run(worker.run_cycle(ordered[-2]))
    assert final.workspaces_examined == 1
    assert final.next_cursor is None


def test_a_failing_workspace_does_not_block_later_ones_and_is_retried(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Prune poison")
    account = _account(client, headers, "Poison card")
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Poison Shop"))
    preview = _preview(client, headers, [transaction_id])
    asyncio.run(_expire(preview["id"]))
    poisoned = uuid.UUID(identity["workspace"]["id"])

    async def all_ids() -> list[uuid.UUID]:
        async with AsyncSessionFactory() as session:
            return await preview_repository.list_workspace_ids_after(
                session, after_id=None, limit=1000
            )

    ordered = asyncio.run(all_ids())
    assert poisoned in ordered
    later = ordered[ordered.index(poisoned) + 1 :]

    original = worker.service.prune_workspace_previews
    attempts: list[uuid.UUID] = []

    async def failing(session, workspace_id, *, now, batch_size):
        attempts.append(workspace_id)
        if workspace_id == poisoned:
            raise RuntimeError("simulated workspace failure")
        return await original(session, workspace_id, now=now, batch_size=batch_size)

    monkeypatch.setattr(worker.service, "prune_workspace_previews", failing)
    result = asyncio.run(worker.run_cycle(None))

    assert result.workspaces_failed == 1
    # The cursor moved past the failure, so everything after it was still examined.
    assert [item for item in attempts if item in later] == later
    assert asyncio.run(_preview_exists(preview["id"])) is True

    # The next rotation retries it, and with the failure gone the preview is reclaimed.
    monkeypatch.setattr(worker.service, "prune_workspace_previews", original)
    retried = asyncio.run(worker.run_cycle(None))
    assert retried.workspaces_failed == 0
    assert asyncio.run(_preview_exists(preview["id"])) is False


def test_empty_cycle_succeeds_and_repeats_cleanly(client: TestClient) -> None:
    _register(client, "Prune empty")
    first = asyncio.run(worker.run_cycle(None))
    assert first.workspaces_failed == 0
    assert first.previews_deleted == 0
    second = asyncio.run(worker.run_cycle(None))
    assert second.previews_deleted == 0
    assert second.workspaces_examined == first.workspaces_examined


def test_once_mode_runs_one_cycle_and_reports_exit_codes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Prune once")
    account = _account(client, headers, "Once card")
    transaction_id = asyncio.run(_insert_transaction(identity, account["id"], "Once Shop"))
    preview = _preview(client, headers, [transaction_id])
    asyncio.run(_expire(preview["id"]))

    cycles: list[uuid.UUID | None] = []
    original = worker.run_cycle

    async def counting(cursor):
        cycles.append(cursor)
        return await original(cursor)

    monkeypatch.setattr(worker, "run_cycle", counting)
    # Replace the engine reference itself: disposing the real one would close the pool the rest of
    # the suite shares, and AsyncEngine.dispose is read-only so it cannot be patched in place.
    monkeypatch.setattr(worker, "engine", _StubEngine())

    assert asyncio.run(worker.run_once()) == 0
    assert cycles == [None]
    assert asyncio.run(_preview_exists(preview["id"])) is False

    # Disabled: no cycle, no deletion, still a clean exit.
    second = _preview(client, headers, [transaction_id])
    asyncio.run(_expire(second["id"]))
    cycles.clear()
    monkeypatch.setattr(settings, "categorization_prune_enabled", False)
    assert asyncio.run(worker.run_once()) == 0
    assert cycles == []
    assert asyncio.run(_preview_exists(second["id"])) is True

    # A cycle-wide failure is fatal for --once only.
    monkeypatch.setattr(settings, "categorization_prune_enabled", True)

    async def exploding(_cursor):
        raise RuntimeError("simulated enumeration failure")

    monkeypatch.setattr(worker, "run_cycle", exploding)
    assert asyncio.run(worker.run_once()) == 1


def test_daemon_survives_a_fatal_cycle_and_stops_promptly(monkeypatch: pytest.MonkeyPatch) -> None:
    """One broken cycle must not end the daemon, and STOP must cut the sleep short.

    A failed cycle deliberately backs off through the normal poll sleep rather than hot-looping, so
    the interval is shortened here to let the recovery cycle arrive within the test.
    """
    monkeypatch.setattr(settings, "categorization_prune_poll_seconds", 0.05)

    calls: list[int] = []

    async def flaky(_cursor):
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("simulated enumeration failure")
        # The daemon recovered. Ask it to stop while it waits on the poll timer.
        worker.STOP.set()
        return worker.CycleResult()

    stub = _StubEngine()
    monkeypatch.setattr(worker, "run_cycle", flaky)
    monkeypatch.setattr(worker, "engine", stub)

    async def drive() -> None:
        await asyncio.wait_for(worker.run(), timeout=10)

    asyncio.run(drive())
    # The fatal first cycle was absorbed and a second cycle still ran.
    assert calls == [0, 1]
    assert stub.disposed is True


def test_stop_interrupts_a_long_poll_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """STOP raised during the poll sleep ends the daemon immediately, not an hour later.

    The discriminator is the cycle count: a daemon that was genuinely parked on the interruptible
    timer runs exactly one cycle across the whole test, while a hot loop would run many and a
    non-interruptible sleep would never return.
    """
    monkeypatch.setattr(settings, "categorization_prune_poll_seconds", 3600)
    worker.STOP.clear()

    cycles: list[int] = []

    async def empty(_cursor):
        cycles.append(len(cycles))
        return worker.CycleResult()

    stub = _StubEngine()
    monkeypatch.setattr(worker, "run_cycle", empty)
    monkeypatch.setattr(worker, "engine", stub)

    async def drive() -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.create_task(worker.run())
        # Let the first cycle finish and the daemon settle onto its one-hour timer.
        await asyncio.sleep(0.2)
        worker.STOP.set()
        await asyncio.wait_for(task, timeout=10)
        return loop.time() - started

    elapsed = asyncio.run(drive())
    assert cycles == [0]
    assert elapsed < 5
    assert stub.disposed is True


def test_stop_during_a_cycle_finishes_the_batch_and_starts_no_new_workspace(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        _register(client, f"Prune shutdown {index}")

    monkeypatch.setattr(settings, "categorization_prune_max_workspaces_per_cycle", 500)
    original = worker.service.prune_workspace_previews
    examined: list[uuid.UUID] = []

    async def stop_after_first(session, workspace_id, *, now, batch_size):
        examined.append(workspace_id)
        deleted = await original(session, workspace_id, now=now, batch_size=batch_size)
        # Shutdown arrives while this batch is committing; it must still complete.
        worker.STOP.set()
        return deleted

    monkeypatch.setattr(worker.service, "prune_workspace_previews", stop_after_first)
    result = asyncio.run(worker.run_cycle(None))

    assert len(examined) == 1
    assert result.workspaces_examined == 1
    assert result.workspaces_failed == 0
    # The cursor points at the workspace that was actually finished, so nothing is skipped later.
    assert result.next_cursor == examined[0]


def test_prune_service_rejects_a_naive_instant(client: TestClient) -> None:
    identity, _headers = _register(client, "Prune naive")

    async def run() -> str:
        async with AsyncSessionFactory() as session:
            try:
                await preview_service.prune_workspace_previews(
                    session,
                    uuid.UUID(identity["workspace"]["id"]),
                    now=datetime(2026, 9, 1, 12, 0),
                    batch_size=10,
                )
            except ValueError as error:
                return str(error)
            raise AssertionError("A naive instant was accepted")

    assert "timezone-aware" in asyncio.run(run())


def test_workspace_keyset_paging_is_bounded_and_ordered(client: TestClient) -> None:
    for index in range(3):
        _register(client, f"Prune paging {index}")

    async def page(after: uuid.UUID | None, limit: int) -> list[uuid.UUID]:
        async with AsyncSessionFactory() as session:
            return await preview_repository.list_workspace_ids_after(
                session, after_id=after, limit=limit
            )

    everything = asyncio.run(page(None, 1000))
    assert everything == sorted(everything)
    assert len(asyncio.run(page(None, 2))) == 2
    # Strictly greater than the cursor: no workspace is returned twice across pages.
    assert asyncio.run(page(everything[0], 1000)) == everything[1:]
    assert asyncio.run(page(everything[-1], 1000)) == []
