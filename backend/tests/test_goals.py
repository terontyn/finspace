import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.audit import AuditLog
from app.db.models.goals import GoalCommandResult, GoalContribution
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.dependencies.context import RequestContext
from app.schemas.goals import GoalContributionCreate, GoalCorrectionCreate, GoalUpdate
from app.services import goals as goal_service
from app.services.month_close_fingerprint import financial_fingerprint
from tests.test_automations import _register


@pytest.fixture(autouse=True)
def _configure_goal_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _key(prefix: str = "goal") -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _goal(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    currency: str = "RUB",
    target_amount: str = "100.0000",
    target_date: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/goals",
        headers={**headers, "X-Idempotency-Key": key or _key("create")},
        json={
            "name": name or f"Goal {uuid.uuid4().hex[:8]}",
            "description": "Explicit planning progress",
            "currency": currency,
            "target_amount": target_amount,
            "target_date": target_date,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _contribute(
    client: TestClient,
    headers: dict[str, str],
    goal_id: str,
    amount: str,
    *,
    contributed_at: str | None = None,
    note: str | None = None,
    key: str | None = None,
) -> Response:
    return client.post(
        f"/api/v1/goals/{goal_id}/contributions",
        headers={**headers, "X-Idempotency-Key": key or _key("contribution")},
        json={"amount": amount, "contributed_at": contributed_at, "note": note},
    )


def _correct(
    client: TestClient,
    headers: dict[str, str],
    goal_id: str,
    contribution_id: str,
    adjustment: str,
    *,
    key: str | None = None,
) -> Response:
    return client.post(
        f"/api/v1/goals/{goal_id}/contributions/{contribution_id}/correct",
        headers={**headers, "X-Idempotency-Key": key or _key("correction")},
        json={"adjustment_amount": adjustment, "note": "Append-only correction"},
    )


def _action(
    client: TestClient,
    headers: dict[str, str],
    goal: dict[str, Any],
    action: str,
    *,
    key: str | None = None,
) -> Response:
    return client.post(
        f"/api/v1/goals/{goal['id']}/{action}",
        headers={**headers, "X-Idempotency-Key": key or _key(action)},
        json={"version": goal["version"]},
    )


def test_create_list_filters_workspace_roles_patch_and_currency_immutability(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Goal basics")
    missing_key = client.post(
        "/api/v1/goals",
        headers=headers,
        json={"name": "No key", "currency": "RUB", "target_amount": "1"},
    )
    assert missing_key.status_code == 422

    rub = _goal(client, headers, name="Emergency reserve", target_date="2030-01-01")
    usd = _goal(client, headers, name="USD trip", currency="USD")
    assert rub["status"] == "active"
    assert rub["version"] == 1
    assert rub["contributed_amount"] == "0.0000"
    assert rub["progress_percent"] == "0.0000"

    listed = client.get("/api/v1/goals?currency=RUB&search=reserve", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [rub["id"]]
    assert (
        client.get("/api/v1/goals?status=completed", headers=headers).json()["page"]["total"] == 0
    )

    patch = client.patch(
        f"/api/v1/goals/{rub['id']}",
        headers={**headers, "X-Idempotency-Key": _key("patch")},
        json={"version": rub["version"], "currency": "EUR", "target_amount": "50"},
    )
    assert patch.status_code == 200, patch.text
    changed = patch.json()
    assert changed["currency"] == "EUR"
    assert changed["version"] == 2

    stale = client.patch(
        f"/api/v1/goals/{rub['id']}",
        headers={**headers, "X-Idempotency-Key": _key("stale")},
        json={"version": 1, "name": "Stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "GOAL_VERSION_CONFLICT"

    event = _contribute(client, headers, rub["id"], "10")
    assert event.status_code == 201, event.text
    immutable = client.patch(
        f"/api/v1/goals/{rub['id']}",
        headers={**headers, "X-Idempotency-Key": _key("currency")},
        json={"version": changed["version"], "currency": "RUB"},
    )
    assert immutable.status_code == 409
    assert immutable.json()["error"]["code"] == "GOAL_CURRENCY_IMMUTABLE"

    _, other_headers = _register(client, "Other Goal workspace")
    isolated = client.get(f"/api/v1/goals/{rub['id']}", headers=other_headers)
    assert isolated.status_code == 404
    assert client.get(f"/api/v1/goals/{usd['id']}", headers=headers).status_code == 200

    async def role_headers() -> tuple[dict[str, str], dict[str, str]]:
        async with AsyncSessionFactory() as session:
            viewer_email = f"viewer-{uuid.uuid4()}@test.local"
            editor_email = f"editor-{uuid.uuid4()}@test.local"
            viewer = User(
                email=viewer_email,
                normalized_email=viewer_email,
                display_name="Goal Viewer",
                locale="ru-RU",
                timezone="Asia/Yekaterinburg",
                is_active=True,
                password_hash=None,
                email_verified=True,
            )
            editor = User(
                email=editor_email,
                normalized_email=editor_email,
                display_name="Goal Editor",
                locale="ru-RU",
                timezone="Asia/Yekaterinburg",
                is_active=True,
                password_hash=None,
                email_verified=True,
            )
            session.add_all([viewer, editor])
            await session.flush()
            workspace_id = uuid.UUID(headers["X-Workspace-ID"])
            session.add_all(
                [
                    WorkspaceMember(workspace_id=workspace_id, user_id=viewer.id, role="viewer"),
                    WorkspaceMember(workspace_id=workspace_id, user_id=editor.id, role="editor"),
                ]
            )
            await session.commit()
            return (
                {"X-User-ID": str(viewer.id), "X-Workspace-ID": str(workspace_id)},
                {"X-User-ID": str(editor.id), "X-Workspace-ID": str(workspace_id)},
            )

    viewer_headers, editor_headers = asyncio.run(role_headers())
    assert client.get("/api/v1/goals", headers=viewer_headers).status_code == 200
    denied = client.post(
        "/api/v1/goals",
        headers={**viewer_headers, "X-Idempotency-Key": _key("viewer")},
        json={"name": "Denied", "currency": "RUB", "target_amount": "1"},
    )
    assert denied.status_code == 403
    assert _goal(client, editor_headers, name="Editor Goal")["name"] == "Editor Goal"


def test_lifecycle_explicit_completion_soft_delete_restore_and_terminal_cancel(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Goal lifecycle")
    goal = _goal(client, headers)
    first = _contribute(client, headers, goal["id"], "60").json()
    assert first["goal"]["status"] == "active"
    assert first["goal"]["is_target_reached"] is False

    premature = _action(client, headers, first["goal"], "complete")
    assert premature.status_code == 409
    assert premature.json()["error"]["code"] == "GOAL_TARGET_NOT_REACHED"

    paused = _action(client, headers, first["goal"], "pause")
    assert paused.status_code == 200
    blocked = _contribute(client, headers, goal["id"], "1")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "GOAL_CONTRIBUTION_NOT_ALLOWED"

    corrected = _correct(client, headers, goal["id"], first["contribution"]["id"], "40")
    assert corrected.status_code == 201, corrected.text
    reached = corrected.json()["goal"]
    assert reached["status"] == "paused"
    assert reached["is_target_reached"] is True

    completed = _action(client, headers, reached, "complete")
    assert completed.status_code == 200
    lowered = _correct(client, headers, goal["id"], first["contribution"]["id"], "-1")
    assert lowered.status_code == 201
    assert lowered.json()["goal"]["status"] == "completed"
    assert lowered.json()["goal"]["is_target_reached"] is False
    assert _contribute(client, headers, goal["id"], "1").status_code == 409

    reopened = _action(client, headers, lowered.json()["goal"], "reopen")
    assert reopened.status_code == 200
    cancelled = _action(client, headers, reopened.json(), "cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert _action(client, headers, cancelled.json(), "resume").status_code == 409
    assert _action(client, headers, cancelled.json(), "reopen").status_code == 409

    delete_key = _key("delete")
    deleted = client.delete(
        f"/api/v1/goals/{goal['id']}",
        headers={**headers, "X-Idempotency-Key": delete_key},
        params={"version": cancelled.json()["version"]},
    )
    assert deleted.status_code == 200
    deleted_body = deleted.json()
    assert deleted_body["deleted_at"] is not None
    assert deleted_body["status"] == "cancelled"
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=headers).status_code == 404

    replay = client.delete(
        f"/api/v1/goals/{goal['id']}",
        headers={**headers, "X-Idempotency-Key": delete_key},
        params={"version": cancelled.json()["version"]},
    )
    assert replay.status_code == 200
    assert replay.json() == deleted_body

    restored = client.post(
        f"/api/v1/goals/{goal['id']}/restore",
        headers={**headers, "X-Idempotency-Key": _key("restore")},
        json={"version": deleted_body["version"]},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "cancelled"
    assert restored.json()["deleted_at"] is None


def test_correction_invariants_history_and_exact_terminal_replay(client: TestClient) -> None:
    _, headers = _register(client, "Goal correction")
    goal = _goal(client, headers, target_amount="150")
    original_key = _key("original")
    original = _contribute(client, headers, goal["id"], "100", key=original_key)
    assert original.status_code == 201
    original_body = original.json()
    other = _contribute(client, headers, goal["id"], "100")
    assert other.status_code == 201
    assert other.json()["goal"]["progress_percent"] == "133.3333"
    assert other.json()["goal"]["remaining_amount"] == "-50.0000"

    zeroed = _correct(
        client,
        headers,
        goal["id"],
        original_body["contribution"]["id"],
        "-100",
    )
    assert zeroed.status_code == 201, zeroed.text
    overcorrect = _correct(
        client,
        headers,
        goal["id"],
        original_body["contribution"]["id"],
        "-0.0001",
    )
    assert overcorrect.status_code == 422
    assert overcorrect.json()["error"]["code"] == "GOAL_CORRECTION_INVALID"

    positive = _correct(
        client,
        headers,
        goal["id"],
        original_body["contribution"]["id"],
        "5",
    )
    assert positive.status_code == 201
    correction_id = positive.json()["contribution"]["id"]
    correction_of_correction = _correct(client, headers, goal["id"], correction_id, "1")
    assert correction_of_correction.status_code == 409

    history = client.get(f"/api/v1/goals/{goal['id']}/contributions?limit=2", headers=headers)
    assert history.status_code == 200
    assert history.json()["page"]["total"] == 4
    assert len(history.json()["items"]) == 2
    assert history.json()["items"][0]["created_by_display_name"] is not None

    replay = _contribute(client, headers, goal["id"], "100", key=original_key)
    assert replay.status_code == 201
    assert replay.json() == original_body
    assert replay.json()["goal"]["contributed_amount"] == "100.0000"
    live = client.get(f"/api/v1/goals/{goal['id']}", headers=headers).json()
    assert live["contributed_amount"] == "105.0000"

    collision = _contribute(client, headers, goal["id"], "101", key=original_key)
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "GOAL_IDEMPOTENCY_CONFLICT"

    async def evidence_counts() -> tuple[int, int, int]:
        async with AsyncSessionFactory() as session:
            goal_uuid = uuid.UUID(goal["id"])
            workspace_uuid = uuid.UUID(headers["X-Workspace-ID"])
            event_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(GoalContribution)
                    .where(GoalContribution.goal_id == goal_uuid)
                )
                or 0
            )
            result_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(GoalCommandResult)
                    .where(
                        GoalCommandResult.workspace_id == workspace_uuid,
                        GoalCommandResult.idempotency_key == original_key,
                    )
                )
                or 0
            )
            audit_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.workspace_id == workspace_uuid,
                        AuditLog.entity_type == "goal_contribution",
                        AuditLog.entity_id == uuid.UUID(original_body["contribution"]["id"]),
                    )
                )
                or 0
            )
            return event_count, result_count, audit_count

    assert asyncio.run(evidence_counts()) == (4, 1, 1)


def test_dates_overdue_closed_month_and_soft_deleted_semantics(client: TestClient) -> None:
    _, headers = _register(client, "Goal dates")
    local_today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Yekaterinburg")).date()
    goal = _goal(
        client,
        headers,
        target_date=(local_today - timedelta(days=1)).isoformat(),
    )
    assert goal["days_remaining"] <= 0
    assert goal["overdue"] is True

    past = _contribute(client, headers, goal["id"], "1", contributed_at="2020-01-01T00:00:00Z")
    assert past.status_code == 201
    within_skew = _contribute(
        client,
        headers,
        goal["id"],
        "1",
        contributed_at=(datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
    )
    assert within_skew.status_code == 201
    future = _contribute(
        client,
        headers,
        goal["id"],
        "1",
        contributed_at=(datetime.now(UTC) + timedelta(minutes=6)).isoformat(),
    )
    assert future.status_code == 422
    assert future.json()["error"]["code"] == "GOAL_CONTRIBUTION_INVALID"

    prepared = client.post("/api/v1/month-close/2026/7/prepare", headers=headers, json={})
    assert prepared.status_code == 200, prepared.text
    closed = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": _key("close")},
        json={
            "version": prepared.json()["version"],
            "confirm": True,
            "prepare_token": prepared.json()["prepare_token"],
        },
    )
    assert closed.status_code == 200, closed.text
    closed_month_event = _contribute(
        client, headers, goal["id"], "1", contributed_at="2026-07-15T12:00:00Z"
    )
    assert closed_month_event.status_code == 201, closed_month_event.text

    paused = _action(client, headers, closed_month_event.json()["goal"], "pause")
    assert paused.json()["overdue"] is True
    deleted = client.delete(
        f"/api/v1/goals/{goal['id']}",
        headers={**headers, "X-Idempotency-Key": _key("delete-date")},
        params={"version": paused.json()["version"]},
    )
    assert deleted.json()["overdue"] is False


def test_stable_pagination_and_budget_month_close_fingerprint_isolation(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Goal isolation")
    first = _goal(client, headers, name="A active", target_date="2030-02-01")
    second = _goal(client, headers, name="B active", target_date="2030-01-01")
    third = _goal(client, headers, name="C paused")
    paused = _action(client, headers, third, "pause")
    assert paused.status_code == 200
    fourth = _goal(client, headers, name="D cancelled")
    cancelled = _action(client, headers, fourth, "cancel")
    assert cancelled.status_code == 200

    full = client.get("/api/v1/goals?limit=100", headers=headers).json()["items"]
    paged: list[dict[str, Any]] = []
    for offset in range(len(full)):
        page = client.get(f"/api/v1/goals?limit=1&offset={offset}", headers=headers)
        assert page.status_code == 200
        paged.extend(page.json()["items"])
    assert [item["id"] for item in paged] == [item["id"] for item in full]
    assert [item["id"] for item in full[:2]] == [second["id"], first["id"]]
    assert full[2]["status"] == "paused"
    assert full[3]["status"] == "cancelled"

    budget = client.put(
        "/api/v1/budgets/2026-08/RUB",
        headers={**headers, "X-Idempotency-Key": _key("budget")},
        json={
            "version": None,
            "planned_income": "1000",
            "rollover_policy": "none",
            "allocations": [],
        },
    )
    assert budget.status_code == 200, budget.text
    budget_before = client.get("/api/v1/budgets/2026-08/RUB", headers=headers).json()

    async def fingerprint() -> str:
        async with AsyncSessionFactory() as session:
            return await financial_fingerprint(
                session,
                uuid.UUID(headers["X-Workspace-ID"]),
                datetime(2026, 9, 1, tzinfo=UTC),
            )

    fingerprint_before = asyncio.run(fingerprint())
    event = _contribute(client, headers, first["id"], "25", contributed_at="2026-08-10T10:00:00Z")
    assert event.status_code == 201
    fingerprint_after = asyncio.run(fingerprint())
    budget_after = client.get("/api/v1/budgets/2026-08/RUB", headers=headers).json()
    assert fingerprint_after == fingerprint_before
    assert budget_after == budget_before


def test_command_atomicity_rolls_back_domain_audit_and_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, headers = _register(client, "Goal atomicity")
    goal = _goal(client, headers)
    original_record_result = goal_service._record_result

    async def fail_result(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected command result failure")

    monkeypatch.setattr(goal_service, "_record_result", fail_result)
    response = _contribute(client, headers, goal["id"], "25")
    assert response.status_code == 500
    monkeypatch.setattr(goal_service, "_record_result", original_record_result)
    live = client.get(f"/api/v1/goals/{goal['id']}", headers=headers).json()
    assert live["contribution_count"] == 0
    assert live["contributed_amount"] == "0.0000"

    original_audit = goal_service.record_audit

    async def fail_audit(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(goal_service, "record_audit", fail_audit)
    create = client.post(
        "/api/v1/goals",
        headers={**headers, "X-Idempotency-Key": _key("audit-fail")},
        json={"name": "Must roll back", "currency": "RUB", "target_amount": "1"},
    )
    assert create.status_code == 500
    monkeypatch.setattr(goal_service, "record_audit", original_audit)
    searched = client.get("/api/v1/goals?search=Must%20roll%20back", headers=headers)
    assert searched.json()["page"]["total"] == 0


def test_database_currency_and_cross_goal_correction_constraints(client: TestClient) -> None:
    _, headers = _register(client, "Goal DB constraints")
    first_goal = _goal(client, headers, name="First")
    second_goal = _goal(client, headers, name="Second")
    original = _contribute(client, headers, first_goal["id"], "10").json()["contribution"]

    async def assert_constraints() -> None:
        now = datetime.now(UTC)
        user_id = uuid.UUID(original["created_by"])
        workspace_id = uuid.UUID(headers["X-Workspace-ID"])
        async with AsyncSessionFactory() as session:
            session.add(
                GoalContribution(
                    workspace_id=workspace_id,
                    goal_id=uuid.UUID(first_goal["id"]),
                    currency="USD",
                    amount=Decimal("1"),
                    note=None,
                    contributed_at=now,
                    correction_of_id=None,
                    created_by=user_id,
                    request_id=None,
                    created_at=now,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            session.add(
                GoalContribution(
                    workspace_id=workspace_id,
                    goal_id=uuid.UUID(second_goal["id"]),
                    currency="RUB",
                    amount=Decimal("-1"),
                    note=None,
                    contributed_at=now,
                    correction_of_id=uuid.UUID(original["id"]),
                    created_by=user_id,
                    request_id=None,
                    created_at=now,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(assert_constraints())


def test_postgresql_parallel_contributions_corrections_and_currency_race(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Goal concurrency")
    goal = _goal(client, headers)
    original = _contribute(client, headers, goal["id"], "100").json()["contribution"]

    async def context(session: Any) -> RequestContext:
        user = await session.get(User, uuid.UUID(original["created_by"]))
        workspace = await session.get(Workspace, uuid.UUID(headers["X-Workspace-ID"]))
        assert user is not None and workspace is not None
        return RequestContext(
            user=user,
            workspace=workspace,
            role="owner",
            request_id=str(uuid.uuid4()),
        )

    async def contribute(amount: str) -> object:
        async with AsyncSessionFactory() as session:
            return await goal_service.add_contribution(
                session,
                await context(session),
                uuid.UUID(goal["id"]),
                GoalContributionCreate(amount=Decimal(amount)),
                _key("parallel"),
            )

    async def correct(amount: str) -> object:
        async with AsyncSessionFactory() as session:
            try:
                return await goal_service.correct_contribution(
                    session,
                    await context(session),
                    uuid.UUID(goal["id"]),
                    uuid.UUID(original["id"]),
                    GoalCorrectionCreate(adjustment_amount=Decimal(amount)),
                    _key("parallel-correction"),
                )
            except ApiError as exc:
                return exc

    async def parallel_contributions() -> tuple[object, object]:
        return await asyncio.gather(contribute("10"), contribute("15"))

    first, second = asyncio.run(parallel_contributions())
    assert not isinstance(first, ApiError)
    assert not isinstance(second, ApiError)
    live = client.get(f"/api/v1/goals/{goal['id']}", headers=headers).json()
    assert live["contributed_amount"] == "125.0000"

    async def parallel_corrections() -> tuple[object, object]:
        return await asyncio.gather(correct("-60"), correct("-60"))

    corrections = asyncio.run(parallel_corrections())
    assert sum(isinstance(item, ApiError) for item in corrections) == 1
    error = next(item for item in corrections if isinstance(item, ApiError))
    assert error.code == "GOAL_CORRECTION_INVALID"

    currency_goal = _goal(client, headers, name="Currency race")

    async def currency_update() -> object:
        async with AsyncSessionFactory() as session:
            try:
                return await goal_service.update_goal(
                    session,
                    await context(session),
                    uuid.UUID(currency_goal["id"]),
                    GoalUpdate(version=1, currency="USD"),
                    _key("parallel-currency"),
                )
            except ApiError as exc:
                return exc

    async def currency_contribution() -> object:
        async with AsyncSessionFactory() as session:
            return await goal_service.add_contribution(
                session,
                await context(session),
                uuid.UUID(currency_goal["id"]),
                GoalContributionCreate(amount=Decimal("1")),
                _key("parallel-first-event"),
            )

    async def parallel_currency_change() -> tuple[object, object]:
        return await asyncio.gather(currency_update(), currency_contribution())

    update_result, contribution_result = asyncio.run(parallel_currency_change())
    assert not isinstance(contribution_result, ApiError)
    if isinstance(update_result, ApiError):
        assert update_result.code == "GOAL_CURRENCY_IMMUTABLE"
    final_goal = client.get(f"/api/v1/goals/{currency_goal['id']}", headers=headers).json()
    final_history = client.get(
        f"/api/v1/goals/{currency_goal['id']}/contributions", headers=headers
    ).json()["items"]
    assert final_history[0]["currency"] == final_goal["currency"]

    def lifecycle_race(action: str) -> tuple[object, object]:
        raced_goal = _goal(client, headers, name=f"{action} race")

        async def lifecycle() -> object:
            async with AsyncSessionFactory() as session:
                current_context = await context(session)
                handler = (
                    goal_service.cancel_goal if action == "cancel" else goal_service.pause_goal
                )
                return await handler(
                    session,
                    current_context,
                    uuid.UUID(raced_goal["id"]),
                    1,
                    _key(f"parallel-{action}"),
                )

        async def racing_contribution() -> object:
            async with AsyncSessionFactory() as session:
                try:
                    return await goal_service.add_contribution(
                        session,
                        await context(session),
                        uuid.UUID(raced_goal["id"]),
                        GoalContributionCreate(amount=Decimal("1")),
                        _key(f"parallel-{action}-event"),
                    )
                except ApiError as exc:
                    return exc

        async def race() -> tuple[object, object]:
            return await asyncio.gather(lifecycle(), racing_contribution())

        lifecycle_result, contribution_race_result = asyncio.run(race())
        live_raced = client.get(f"/api/v1/goals/{raced_goal['id']}", headers=headers).json()
        assert live_raced["status"] == ("cancelled" if action == "cancel" else "paused")
        if isinstance(contribution_race_result, ApiError):
            assert contribution_race_result.code == "GOAL_CONTRIBUTION_NOT_ALLOWED"
            assert live_raced["contribution_count"] == 0
        else:
            assert live_raced["contribution_count"] == 1
        return lifecycle_result, contribution_race_result

    cancel_result, _ = lifecycle_race("cancel")
    pause_result, _ = lifecycle_race("pause")
    assert not isinstance(cancel_result, ApiError)
    assert not isinstance(pause_result, ApiError)


def test_goal_migration_scope_and_immutable_model_contract() -> None:
    migration = (Path(__file__).parents[1] / "alembic/versions/0010_goals.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | None = "0009_budget_planning_core"' in migration
    assert "ck_audit_log_action" not in migration
    assert "DELETE FROM audit_log" not in migration
    assert "budget_periods" not in migration
    assert "month_closures" not in migration
    assert "financial_transactions" not in migration
    assert not hasattr(GoalContribution, "version")
    assert not hasattr(GoalContribution, "updated_at")
    assert not hasattr(GoalContribution, "deleted_at")
