import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.audit import AuditLog
from app.db.models.categories import Category
from app.db.models.categorization_apply_operations import (
    CategorizationApplyOperation,
    CategorizationApplyResult,
)
from app.db.models.categorization_previews import CategorizationPreview, CategorizationPreviewItem
from app.db.models.google_sync import SyncOutbox
from app.db.models.transactions import FinancialTransaction, TransactionSplit
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.dependencies.context import RequestContext
from app.repositories import categorization_previews as preview_repository
from app.schemas.categorization_apply import CategorizationApplyRequest
from app.services import categorization_apply as apply_service
from app.services import categorization_rules as rule_service
from app.services.financial_period_guard import get_or_create_control

PASSWORD = "correct horse battery staple"
PREVIEWS = "/api/v1/categorization-previews"


@pytest.fixture(autouse=True)
def _configure_apply_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _register(client: TestClient, label: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"apply-{uuid.uuid4()}@example.com",
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


def _category(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    category_type: str = "expense",
) -> dict:
    response = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": name, "category_type": category_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _rule(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    category_id: str,
    priority: int = 100,
    **matchers: object,
) -> dict:
    response = client.post(
        "/api/v1/categorization-rules",
        headers=headers,
        json={"name": name, "priority": priority, "category_id": category_id, **matchers},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_transaction(
    identity: dict,
    account_id: str,
    *,
    counterparty: str,
    transaction_type: str = "expense",
    status: str = "confirmed",
    category_id: str | None = None,
    target_account_id: str | None = None,
    occurred_at: datetime | None = None,
) -> str:
    async with AsyncSessionFactory() as session:
        transaction = FinancialTransaction(
            workspace_id=uuid.UUID(identity["workspace"]["id"]),
            occurred_at=occurred_at or datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            transaction_type=transaction_type,
            amount=Decimal("1250.2500"),
            currency="RUB",
            account_id=uuid.UUID(account_id),
            target_account_id=uuid.UUID(target_account_id) if target_account_id else None,
            category_id=uuid.UUID(category_id) if category_id else None,
            counterparty=counterparty,
            status=status,
            source="import",
            created_by=uuid.UUID(identity["user"]["id"]),
            updated_by=uuid.UUID(identity["user"]["id"]),
        )
        session.add(transaction)
        await session.commit()
        await session.refresh(transaction)
        return str(transaction.id)


async def _set_role(identity: dict, workspace_id: str, role: str) -> None:
    async with AsyncSessionFactory() as session:
        member = await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == uuid.UUID(workspace_id),
                WorkspaceMember.user_id == uuid.UUID(identity["user"]["id"]),
            )
        )
        assert member is not None
        member.role = role
        await session.commit()


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


async def _transaction_row(transaction_id: str) -> FinancialTransaction:
    async with AsyncSessionFactory() as session:
        row = await session.get(FinancialTransaction, uuid.UUID(transaction_id))
        assert row is not None
        return row


async def _audit_count(transaction_id: str) -> int:
    async with AsyncSessionFactory() as session:
        return int(
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


async def _outbox_count(transaction_id: str) -> int:
    async with AsyncSessionFactory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(SyncOutbox)
                .where(SyncOutbox.entity_id == uuid.UUID(transaction_id))
            )
            or 0
        )


async def _results_for(operation_id: str) -> list[CategorizationApplyResult]:
    async with AsyncSessionFactory() as session:
        return list(
            (
                await session.scalars(
                    select(CategorizationApplyResult)
                    .where(CategorizationApplyResult.operation_id == uuid.UUID(operation_id))
                    .order_by(CategorizationApplyResult.sequence)
                )
            ).all()
        )


async def _operation_for(identity: dict, idempotency_key: str) -> CategorizationApplyOperation:
    async with AsyncSessionFactory() as session:
        operation = await session.scalar(
            select(CategorizationApplyOperation).where(
                CategorizationApplyOperation.workspace_id == uuid.UUID(identity["workspace"]["id"]),
                CategorizationApplyOperation.idempotency_key == idempotency_key,
            )
        )
        assert operation is not None
        return operation


async def _expire_preview(preview_id: str) -> None:
    async with AsyncSessionFactory() as session:
        await session.execute(
            update(CategorizationPreview)
            .where(CategorizationPreview.id == uuid.UUID(preview_id))
            .values(
                created_at=datetime.now(UTC) - timedelta(hours=48),
                expires_at=datetime.now(UTC) - timedelta(hours=24),
            )
        )
        await session.commit()


async def _prune_preview(identity: dict, *, now: datetime | None = None) -> int:
    async with AsyncSessionFactory() as session:
        removed = await preview_repository.delete_expired(
            session,
            uuid.UUID(identity["workspace"]["id"]),
            now or datetime.now(UTC),
        )
        await session.commit()
        return removed


async def _preview_storage(preview_id: str) -> tuple[bool, int]:
    async with AsyncSessionFactory() as session:
        preview = await session.get(CategorizationPreview, uuid.UUID(preview_id))
        item_count = int(
            await session.scalar(
                select(func.count())
                .select_from(CategorizationPreviewItem)
                .where(CategorizationPreviewItem.preview_id == uuid.UUID(preview_id))
            )
            or 0
        )
        return preview is not None, item_count


async def _close_through(identity: dict, closed_through: date) -> None:
    async with AsyncSessionFactory() as session:
        control = await get_or_create_control(
            session,
            uuid.UUID(identity["workspace"]["id"]),
            for_update=True,
        )
        control.closed_through = closed_through
        await session.commit()


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


def _apply(
    client: TestClient,
    headers: dict[str, str],
    preview_id: str,
    item_ids: list[str],
    key: str,
):
    return client.post(
        f"{PREVIEWS}/{preview_id}/apply",
        headers={**headers, "X-Idempotency-Key": key},
        json={"item_ids": item_ids},
    )


def _matched_scenario(client: TestClient, label: str, **transaction_kwargs):
    identity, headers = _register(client, label)
    account = _account(client, headers, f"{label} card")
    category = _category(client, headers, f"{label} target")
    rule = _rule(
        client,
        headers,
        name=f"{label} rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_id = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Apply Shop statement",
            **transaction_kwargs,
        )
    )
    preview = _preview(client, headers, [transaction_id])
    items = _items(client, headers, preview["id"])
    return identity, headers, account, category, rule, transaction_id, preview, items


def test_bulk_apply_commits_matched_items_and_persists_terminal_results(
    client: TestClient,
) -> None:
    identity, headers, account, category, _rule_row, transaction_id, preview, items = (
        _matched_scenario(client, "Apply happy")
    )
    assert items[0]["status"] == "matched"

    response = _apply(client, headers, preview["id"], [items[0]["id"]], "key-happy-1")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["preview_id"] == preview["id"]
    assert payload["summary"] == {
        "requested": 1,
        "applied": 1,
        "conflicts": 0,
        "not_applied": 0,
        "failed": 0,
    }
    result = payload["results"][0]
    assert result["status"] == "applied"
    assert result["transaction_id"] == transaction_id
    assert result["transaction_version"] == 2
    assert result["error_code"] is None

    committed = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
    assert committed.json()["category"]["id"] == category["id"]
    assert committed.json()["version"] == 2

    persisted = asyncio.run(_results_for(payload["operation_id"]))
    assert [(row.status, row.sequence) for row in persisted] == [("applied", 0)]
    assert asyncio.run(_audit_count(transaction_id)) == 1
    assert account is not None and identity is not None


def test_request_level_validation_never_becomes_an_item_result(client: TestClient) -> None:
    identity, headers, _account, _category, _rule_row, _transaction_id, preview, items = (
        _matched_scenario(client, "Apply validation")
    )
    item_id = items[0]["id"]

    missing_header = client.post(
        f"{PREVIEWS}/{preview['id']}/apply", headers=headers, json={"item_ids": [item_id]}
    )
    assert missing_header.status_code == 422

    assert _apply(client, headers, preview["id"], [], "key-empty").status_code == 422
    assert _apply(client, headers, preview["id"], [item_id, item_id], "key-dup").status_code == 422
    assert (
        _apply(
            client,
            headers,
            preview["id"],
            [str(uuid.uuid4()) for _ in range(101)],
            "key-many",
        ).status_code
        == 422
    )

    foreign_item = _apply(client, headers, preview["id"], [str(uuid.uuid4())], "key-foreign")
    assert foreign_item.status_code == 422
    assert foreign_item.json()["error"]["code"] == "CATEGORIZATION_PREVIEW_ITEM_NOT_FOUND"

    missing_preview = _apply(client, headers, str(uuid.uuid4()), [item_id], "key-missing")
    assert missing_preview.status_code == 404

    # No operation was created by any rejected request.
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

    assert asyncio.run(operation_count()) == 0


def test_viewer_cannot_apply_but_any_workspace_editor_can(client: TestClient) -> None:
    owner_identity, owner_headers = _register(client, "Apply roles")
    account = _account(client, owner_headers, "Roles card")
    category = _category(client, owner_headers, "Roles target")
    _rule(
        client,
        owner_headers,
        name="Roles rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_id = asyncio.run(
        _insert_transaction(owner_identity, account["id"], counterparty="Apply Shop roles")
    )
    preview = _preview(client, owner_headers, [transaction_id])
    item_id = _items(client, owner_headers, preview["id"])[0]["id"]

    second = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"apply-member-{uuid.uuid4()}@example.com",
            "display_name": "Apply member",
            "password": PASSWORD,
            "workspace_name": "Discarded",
            "base_currency": "RUB",
            "timezone": "Asia/Yekaterinburg",
        },
    )
    member_identity = second.json()
    workspace_id = owner_identity["workspace"]["id"]

    async def join(role: str) -> None:
        async with AsyncSessionFactory() as session:
            session.add(
                WorkspaceMember(
                    workspace_id=uuid.UUID(workspace_id),
                    user_id=uuid.UUID(member_identity["user"]["id"]),
                    role=role,
                )
            )
            await session.commit()

    asyncio.run(join("viewer"))
    member_headers = {
        "Authorization": f"Bearer {member_identity['access_token']}",
        "X-Workspace-ID": workspace_id,
    }
    viewer = _apply(client, member_headers, preview["id"], [item_id], "key-viewer")
    assert viewer.status_code == 403

    asyncio.run(_set_role(member_identity, workspace_id, "editor"))
    # A preview belongs to the workspace: an editor applies one they did not create.
    editor = _apply(client, member_headers, preview["id"], [item_id], "key-editor")
    assert editor.status_code == 200, editor.text
    assert editor.json()["results"][0]["status"] == "applied"


def test_expired_preview_is_rejected_but_a_claimed_operation_still_replays(
    client: TestClient,
) -> None:
    _identity, headers, _account, _category, _rule_row, transaction_id, preview, items = (
        _matched_scenario(client, "Apply expiry")
    )
    first = _apply(client, headers, preview["id"], [items[0]["id"]], "key-expiry")
    assert first.status_code == 200
    assert first.json()["results"][0]["status"] == "applied"

    async def expire() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(CategorizationPreview)
                .where(CategorizationPreview.id == uuid.UUID(preview["id"]))
                .values(
                    created_at=datetime.now(UTC) - timedelta(hours=48),
                    expires_at=datetime.now(UTC) - timedelta(hours=24),
                )
            )
            await session.commit()

    asyncio.run(expire())

    # A fresh key against an expired preview is refused outright.
    fresh = _apply(client, headers, preview["id"], [items[0]["id"]], "key-expiry-new")
    assert fresh.status_code == 410
    assert fresh.json()["error"]["code"] == "CATEGORIZATION_PREVIEW_EXPIRED"

    # The already-claimed key still recovers its persisted results.
    replay = _apply(client, headers, preview["id"], [items[0]["id"]], "key-expiry")
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert asyncio.run(_transaction_row(transaction_id)).version == 2


def test_idempotent_replay_does_not_mutate_or_duplicate_audit_and_outbox(
    client: TestClient,
) -> None:
    _identity, headers, _account, _category, _rule_row, transaction_id, preview, items = (
        _matched_scenario(client, "Apply replay")
    )
    first = _apply(client, headers, preview["id"], [items[0]["id"]], "key-replay")
    assert first.status_code == 200
    audits = asyncio.run(_audit_count(transaction_id))
    outbox = asyncio.run(_outbox_count(transaction_id))

    second = _apply(client, headers, preview["id"], [items[0]["id"]], "key-replay")
    assert second.status_code == 200
    assert second.json() == first.json()
    assert asyncio.run(_transaction_row(transaction_id)).version == 2
    assert asyncio.run(_audit_count(transaction_id)) == audits
    assert asyncio.run(_outbox_count(transaction_id)) == outbox
    assert len(asyncio.run(_results_for(first.json()["operation_id"]))) == 1


def test_same_key_with_a_different_item_set_is_an_idempotency_conflict(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Apply key conflict")
    account = _account(client, headers, "Key card")
    category = _category(client, headers, "Key target")
    _rule(
        client,
        headers,
        name="Key rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    first_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop one")
    )
    second_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop two")
    )
    preview = _preview(client, headers, [first_id, second_id])
    items = _items(client, headers, preview["id"])

    ok = _apply(client, headers, preview["id"], [items[0]["id"]], "key-shared")
    assert ok.status_code == 200

    clash = _apply(client, headers, preview["id"], [items[1]["id"]], "key-shared")
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "CATEGORIZATION_IDEMPOTENCY_CONFLICT"

    # Reordering the same set is the same logical request, because ids are hashed as a set.
    both_forward = _apply(
        client, headers, preview["id"], [items[0]["id"], items[1]["id"]], "key-both"
    )
    assert both_forward.status_code == 200
    both_reversed = _apply(
        client, headers, preview["id"], [items[1]["id"], items[0]["id"]], "key-both"
    )
    assert both_reversed.status_code == 200
    assert both_reversed.json()["operation_id"] == both_forward.json()["operation_id"]
    # Response order still follows the caller's submitted order.
    assert [row["item_id"] for row in both_reversed.json()["results"]] == [
        items[1]["id"],
        items[0]["id"],
    ]


def test_interrupted_operation_resumes_only_unattempted_items(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Apply resume")
    account = _account(client, headers, "Resume card")
    category = _category(client, headers, "Resume target")
    _rule(
        client,
        headers,
        name="Resume rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    ids = [
        asyncio.run(
            _insert_transaction(identity, account["id"], counterparty=f"Apply Shop {index}")
        )
        for index in range(3)
    ]
    preview = _preview(client, headers, ids)
    items = _items(client, headers, preview["id"])
    item_ids = [item["id"] for item in items]
    item_by_id = {item["id"]: item for item in items}
    canonical_item_ids = sorted(item_ids)
    key = "key-partial"

    # Simulate an interruption: the first item committed, the rest were never attempted.
    class SimulatedProcessDeath(BaseException):
        pass

    original_process_item = apply_service._process_item
    attempted = 0

    async def stop_before_second_item(*args: object, **kwargs: object):
        nonlocal attempted
        attempted += 1
        if attempted == 2:
            raise SimulatedProcessDeath
        return await original_process_item(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(apply_service, "_process_item", stop_before_second_item)

    async def start_and_interrupt() -> None:
        async with AsyncSessionFactory() as session:
            await apply_service.apply_preview_items(
                session,
                await _context_for(identity, session),
                uuid.UUID(preview["id"]),
                CategorizationApplyRequest(item_ids=[uuid.UUID(value) for value in item_ids]),
                key,
            )

    with pytest.raises(SimulatedProcessDeath):
        asyncio.run(start_and_interrupt())
    monkeypatch.setattr(apply_service, "_process_item", original_process_item)

    operation = asyncio.run(_operation_for(identity, key))
    assert operation.status == "in_progress"
    assert len(asyncio.run(_results_for(str(operation.id)))) == 1
    first_transaction_id = item_by_id[canonical_item_ids[0]]["transaction_id"]
    assert asyncio.run(_transaction_row(first_transaction_id)).version == 2
    assert asyncio.run(_audit_count(first_transaction_id)) == 1

    resumed = _apply(client, headers, preview["id"], item_ids, key)
    assert resumed.status_code == 200, resumed.text
    statuses = [row["status"] for row in resumed.json()["results"]]
    assert statuses == ["applied", "applied", "applied"]
    # The already terminal item was replayed, not applied twice.
    assert [asyncio.run(_transaction_row(value)).version for value in ids] == [2, 2, 2]
    assert [asyncio.run(_audit_count(value)) for value in ids] == [1, 1, 1]
    assert len(asyncio.run(_results_for(resumed.json()["operation_id"]))) == 3

    async def operation_status() -> tuple[str, bool]:
        async with AsyncSessionFactory() as session:
            row = await session.get(
                CategorizationApplyOperation, uuid.UUID(resumed.json()["operation_id"])
            )
            assert row is not None
            return row.status, row.completed_at is not None

    assert asyncio.run(operation_status()) == ("completed", True)


def test_interrupted_operation_resumes_in_stable_order_after_reordered_retry(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Apply reordered resume")
    account = _account(client, headers, "Reordered resume card")
    category = _category(client, headers, "Reordered resume target")
    _rule(
        client,
        headers,
        name="Reordered resume rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_ids = [
        asyncio.run(
            _insert_transaction(
                identity,
                account["id"],
                counterparty=f"Apply Shop reordered {index}",
            )
        )
        for index in range(3)
    ]
    preview = _preview(client, headers, transaction_ids)
    items = _items(client, headers, preview["id"])
    item_by_id = {item["id"]: item for item in items}
    canonical_item_ids = sorted(item_by_id)
    retry_item_ids = list(reversed(canonical_item_ids))
    key = "key-reordered-interrupted"

    class SimulatedProcessDeath(BaseException):
        pass

    original_process_item = apply_service._process_item
    attempted = 0

    async def stop_before_second_item(*args: object, **kwargs: object):
        nonlocal attempted
        attempted += 1
        if attempted == 2:
            raise SimulatedProcessDeath
        return await original_process_item(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(apply_service, "_process_item", stop_before_second_item)

    async def start_and_interrupt() -> None:
        async with AsyncSessionFactory() as session:
            await apply_service.apply_preview_items(
                session,
                await _context_for(identity, session),
                uuid.UUID(preview["id"]),
                CategorizationApplyRequest(
                    item_ids=[uuid.UUID(value) for value in canonical_item_ids]
                ),
                key,
            )

    with pytest.raises(SimulatedProcessDeath):
        asyncio.run(start_and_interrupt())
    monkeypatch.setattr(apply_service, "_process_item", original_process_item)

    operation = asyncio.run(_operation_for(identity, key))
    initial_results = asyncio.run(_results_for(str(operation.id)))
    assert operation.status == "in_progress"
    assert [(str(row.item_id), row.sequence) for row in initial_results] == [
        (canonical_item_ids[0], 0)
    ]
    first_transaction_id = item_by_id[canonical_item_ids[0]]["transaction_id"]
    assert asyncio.run(_transaction_row(first_transaction_id)).version == 2
    assert asyncio.run(_audit_count(first_transaction_id)) == 1

    resumed = _apply(client, headers, preview["id"], retry_item_ids, key)
    assert resumed.status_code == 200, resumed.text
    assert [row["item_id"] for row in resumed.json()["results"]] == retry_item_ids
    assert [row["status"] for row in resumed.json()["results"]] == [
        "applied",
        "applied",
        "applied",
    ]

    persisted = asyncio.run(_results_for(str(operation.id)))
    assert [(str(row.item_id), row.sequence) for row in persisted] == [
        (item_id, sequence) for sequence, item_id in enumerate(canonical_item_ids)
    ]
    assert len({row.item_id for row in persisted}) == 3
    assert len({row.sequence for row in persisted}) == 3
    for transaction_id in transaction_ids:
        assert asyncio.run(_transaction_row(transaction_id)).version == 2
        assert asyncio.run(_audit_count(transaction_id)) == 1
        assert asyncio.run(_outbox_count(transaction_id)) == 0
    assert (asyncio.run(_operation_for(identity, key))).status == "completed"

    versions = [asyncio.run(_transaction_row(value)).version for value in transaction_ids]
    audits = [asyncio.run(_audit_count(value)) for value in transaction_ids]
    outbox = [asyncio.run(_outbox_count(value)) for value in transaction_ids]
    replay = _apply(client, headers, preview["id"], canonical_item_ids, key)
    assert replay.status_code == 200, replay.text
    assert [row["item_id"] for row in replay.json()["results"]] == canonical_item_ids
    assert [asyncio.run(_transaction_row(value)).version for value in transaction_ids] == versions
    assert [asyncio.run(_audit_count(value)) for value in transaction_ids] == audits
    assert [asyncio.run(_outbox_count(value)) for value in transaction_ids] == outbox


def test_interrupted_operation_survives_expiry_pruning_and_resumes_unattempted_items(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Apply expiry recovery")
    account = _account(client, headers, "Expiry recovery card")
    category = _category(client, headers, "Expiry recovery target")
    _rule(
        client,
        headers,
        name="Expiry recovery rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_ids = [
        asyncio.run(
            _insert_transaction(
                identity,
                account["id"],
                counterparty=f"Apply Shop recovery {index}",
            )
        )
        for index in range(2)
    ]
    preview = _preview(client, headers, transaction_ids)
    items = _items(client, headers, preview["id"])
    item_ids = [item["id"] for item in items]
    item_by_id = {item["id"]: item for item in items}
    canonical_item_ids = sorted(item_ids)
    key = "key-expiry-interrupted"

    class SimulatedProcessDeath(BaseException):
        pass

    original_process_item = apply_service._process_item
    attempted = 0

    async def stop_before_second_item(*args: object, **kwargs: object):
        nonlocal attempted
        attempted += 1
        if attempted == 2:
            raise SimulatedProcessDeath
        return await original_process_item(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(apply_service, "_process_item", stop_before_second_item)

    async def start_and_interrupt() -> None:
        async with AsyncSessionFactory() as session:
            await apply_service.apply_preview_items(
                session,
                await _context_for(identity, session),
                uuid.UUID(preview["id"]),
                CategorizationApplyRequest(item_ids=[uuid.UUID(value) for value in item_ids]),
                key,
            )

    with pytest.raises(SimulatedProcessDeath):
        asyncio.run(start_and_interrupt())
    monkeypatch.setattr(apply_service, "_process_item", original_process_item)

    operation = asyncio.run(_operation_for(identity, key))
    assert operation.status == "in_progress"
    assert len(asyncio.run(_results_for(str(operation.id)))) == 1
    first_transaction_id = item_by_id[canonical_item_ids[0]]["transaction_id"]
    remaining_transaction_id = item_by_id[canonical_item_ids[1]]["transaction_id"]
    assert asyncio.run(_transaction_row(first_transaction_id)).version == 2
    assert asyncio.run(_transaction_row(remaining_transaction_id)).version == 1
    assert asyncio.run(_audit_count(first_transaction_id)) == 1
    assert asyncio.run(_audit_count(remaining_transaction_id)) == 0
    # Sync outbox emission is disabled in this test configuration; recovery must keep that count
    # stable just as strictly as it keeps enabled-environment outbox rows from duplicating.
    assert [asyncio.run(_outbox_count(value)) for value in transaction_ids] == [0, 0]

    asyncio.run(_expire_preview(preview["id"]))
    assert asyncio.run(_prune_preview(identity)) == 0
    assert asyncio.run(_preview_storage(preview["id"])) == (True, 2)

    resumed = _apply(client, headers, preview["id"], item_ids, key)
    assert resumed.status_code == 200, resumed.text
    assert [result["status"] for result in resumed.json()["results"]] == [
        "applied",
        "applied",
    ]
    assert [asyncio.run(_transaction_row(value)).version for value in transaction_ids] == [2, 2]
    assert [asyncio.run(_audit_count(value)) for value in transaction_ids] == [1, 1]
    assert [asyncio.run(_outbox_count(value)) for value in transaction_ids] == [0, 0]
    assert (asyncio.run(_operation_for(identity, key))).status == "completed"

    # A completed operation no longer pins its expired preview; only terminal replay evidence stays.
    assert asyncio.run(_prune_preview(identity)) == 1
    assert asyncio.run(_preview_storage(preview["id"])) == (False, 0)
    assert len(asyncio.run(_results_for(str(operation.id)))) == 2

    versions = [asyncio.run(_transaction_row(value)).version for value in transaction_ids]
    audits = [asyncio.run(_audit_count(value)) for value in transaction_ids]
    outbox = [asyncio.run(_outbox_count(value)) for value in transaction_ids]
    replay = _apply(client, headers, preview["id"], item_ids, key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == resumed.json()
    assert [asyncio.run(_transaction_row(value)).version for value in transaction_ids] == versions
    assert [asyncio.run(_audit_count(value)) for value in transaction_ids] == audits
    assert [asyncio.run(_outbox_count(value)) for value in transaction_ids] == outbox


def test_completed_operation_replays_after_physical_preview_pruning(
    client: TestClient,
) -> None:
    identity, headers, _account_row, _category_row, _rule_row, transaction_id, preview, items = (
        _matched_scenario(client, "Apply completed pruning")
    )
    item_ids = [items[0]["id"]]
    key = "key-completed-pruning"
    first = _apply(client, headers, preview["id"], item_ids, key)
    assert first.status_code == 200, first.text
    operation_id = first.json()["operation_id"]

    asyncio.run(_expire_preview(preview["id"]))
    assert asyncio.run(_prune_preview(identity)) == 1
    assert asyncio.run(_preview_storage(preview["id"])) == (False, 0)
    assert len(asyncio.run(_results_for(operation_id))) == 1

    version = asyncio.run(_transaction_row(transaction_id)).version
    audit_count = asyncio.run(_audit_count(transaction_id))
    outbox_count = asyncio.run(_outbox_count(transaction_id))
    replay = _apply(client, headers, preview["id"], item_ids, key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert asyncio.run(_transaction_row(transaction_id)).version == version
    assert asyncio.run(_audit_count(transaction_id)) == audit_count
    assert asyncio.run(_outbox_count(transaction_id)) == outbox_count

    # A different key cannot recreate an operation after the proposal has been pruned.
    fresh = _apply(client, headers, preview["id"], item_ids, "key-after-pruning")
    assert fresh.status_code == 404
    assert fresh.json()["error"]["code"] == "CATEGORIZATION_PREVIEW_NOT_FOUND"


def test_apply_claim_lock_wins_against_expiry_cleanup(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, _headers, _account_row, _category_row, _rule_row, _transaction_id, preview, items = (
        _matched_scenario(client, "Apply cleanup claim lock")
    )
    claim_entered = asyncio.Event()
    release_claim = asyncio.Event()
    original_claim = apply_service.repository.claim_operation

    async def paused_claim(*args: object, **kwargs: object):
        claim_entered.set()
        await release_claim.wait()
        return await original_claim(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(apply_service.repository, "claim_operation", paused_claim)

    async def race() -> tuple[int, str]:
        async with AsyncSessionFactory() as apply_session:
            apply_task = asyncio.create_task(
                apply_service.apply_preview_items(
                    apply_session,
                    await _context_for(identity, apply_session),
                    uuid.UUID(preview["id"]),
                    CategorizationApplyRequest(item_ids=[uuid.UUID(items[0]["id"])]),
                    "key-claim-lock-wins",
                )
            )
            await asyncio.wait_for(claim_entered.wait(), timeout=5)
            try:
                # Artificial future cutoff makes the still-live preview eligible for this cleanup
                # call while the apply transaction is holding FOR SHARE on its row.
                removed = await asyncio.wait_for(
                    _prune_preview(identity, now=datetime.now(UTC) + timedelta(hours=48)),
                    timeout=5,
                )
            finally:
                release_claim.set()
            outcome = await asyncio.wait_for(apply_task, timeout=5)
            return removed, outcome.results[0].status

    assert asyncio.run(race()) == (0, "applied")
    assert asyncio.run(_preview_storage(preview["id"])) == (True, 1)


def test_cleanup_lock_wins_before_new_apply_claim(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, _headers, _account_row, _category_row, _rule_row, _transaction_id, preview, items = (
        _matched_scenario(client, "Cleanup lock wins")
    )
    asyncio.run(_expire_preview(preview["id"]))
    cleanup_locked = asyncio.Event()
    release_cleanup = asyncio.Event()
    apply_started = asyncio.Event()

    class PausedCleanupSession:
        def __init__(self, session) -> None:
            self.session = session

        async def scalars(self, statement, *args: object, **kwargs: object):
            result = await self.session.scalars(statement, *args, **kwargs)
            cleanup_locked.set()
            await release_cleanup.wait()
            return result

        async def execute(self, statement, *args: object, **kwargs: object):
            return await self.session.execute(statement, *args, **kwargs)

    original_get_preview = preview_repository.get_preview

    async def signaled_get_preview(*args: object, **kwargs: object):
        apply_started.set()
        return await original_get_preview(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(preview_repository, "get_preview", signaled_get_preview)

    async def race() -> tuple[int, str]:
        async with AsyncSessionFactory() as cleanup_session:
            cleanup_task = asyncio.create_task(
                preview_repository.delete_expired(
                    PausedCleanupSession(cleanup_session),  # type: ignore[arg-type]
                    uuid.UUID(identity["workspace"]["id"]),
                    datetime.now(UTC),
                )
            )
            await asyncio.wait_for(cleanup_locked.wait(), timeout=5)

            async def try_apply() -> str:
                async with AsyncSessionFactory() as apply_session:
                    try:
                        await apply_service.apply_preview_items(
                            apply_session,
                            await _context_for(identity, apply_session),
                            uuid.UUID(preview["id"]),
                            CategorizationApplyRequest(item_ids=[uuid.UUID(items[0]["id"])]),
                            "key-cleanup-lock-wins",
                        )
                    except ApiError as exc:
                        return exc.code
                raise AssertionError("Apply unexpectedly created an operation")

            apply_task = asyncio.create_task(try_apply())
            await asyncio.wait_for(apply_started.wait(), timeout=5)
            release_cleanup.set()
            removed = await asyncio.wait_for(cleanup_task, timeout=5)
            await cleanup_session.commit()
            error_code = await asyncio.wait_for(apply_task, timeout=5)
            return removed, error_code

    assert asyncio.run(race()) == (1, "CATEGORIZATION_PREVIEW_NOT_FOUND")
    assert asyncio.run(_preview_storage(preview["id"])) == (False, 0)

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

    assert asyncio.run(operation_count()) == 0


def test_stale_transaction_split_and_category_races_never_overwrite(client: TestClient) -> None:
    identity, headers = _register(client, "Apply races")
    account = _account(client, headers, "Race card")
    category = _category(client, headers, "Race target")
    other = _category(client, headers, "Race other")
    _rule(
        client,
        headers,
        name="Race rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    edited = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop edited")
    )
    split_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop split")
    )
    categorized = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop categorized")
    )
    reconciled = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop reconciled")
    )
    preview = _preview(client, headers, [edited, split_id, categorized, reconciled])
    items = _items(client, headers, preview["id"])
    assert [item["status"] for item in items] == ["matched"] * 4

    async def mutate_after_preview() -> None:
        async with AsyncSessionFactory() as session:
            # 1. plain edit bumps the version
            await session.execute(
                update(FinancialTransaction)
                .where(FinancialTransaction.id == uuid.UUID(edited))
                .values(version=2, counterparty="Apply Shop edited later")
            )
            # 2. splits appear without a version bump, so the split guard must catch it
            session.add(
                TransactionSplit(
                    transaction_id=uuid.UUID(split_id),
                    category_id=uuid.UUID(other["id"]),
                    amount=Decimal("1250.2500"),
                )
            )
            # 3. a category appears without a version bump
            await session.execute(
                update(FinancialTransaction)
                .where(FinancialTransaction.id == uuid.UUID(categorized))
                .values(category_id=uuid.UUID(other["id"]))
            )
            # 4. reconciliation without a version bump
            await session.execute(
                update(FinancialTransaction)
                .where(FinancialTransaction.id == uuid.UUID(reconciled))
                .values(status="reconciled")
            )
            await session.commit()

    asyncio.run(mutate_after_preview())

    response = _apply(client, headers, preview["id"], [item["id"] for item in items], "key-races")
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [row["status"] for row in results] == [
        "transaction_changed",
        "split",
        "already_categorized",
        "reconciled",
    ]
    assert results[0]["error_code"] == "CATEGORIZATION_TRANSACTION_CHANGED"
    assert results[0]["expected_version"] == 1
    assert results[0]["current_version"] == 2
    assert response.json()["summary"]["applied"] == 0
    assert response.json()["summary"]["conflicts"] == 1

    # Nothing was overwritten.
    assert asyncio.run(_transaction_row(edited)).category_id is None
    assert asyncio.run(_transaction_row(split_id)).category_id is None
    assert asyncio.run(_transaction_row(categorized)).category_id == uuid.UUID(other["id"])
    assert asyncio.run(_transaction_row(reconciled)).category_id is None
    for transaction_id in (edited, split_id, categorized, reconciled):
        assert asyncio.run(_audit_count(transaction_id)) == 0


def test_rule_and_category_races_are_reported_without_writing(client: TestClient) -> None:
    identity, headers = _register(client, "Apply rule races")
    account = _account(client, headers, "Rule race card")
    category = _category(client, headers, "Rule race target")
    intruder_category = _category(client, headers, "Rule race intruder")
    archived_category = _category(client, headers, "Rule race archived")
    typed_category = _category(client, headers, "Rule race typed")
    rule = _rule(
        client,
        headers,
        name="Rule race rule",
        category_id=category["id"],
        priority=50,
        counterparty_contains="apply shop",
    )
    archived_rule = _rule(
        client,
        headers,
        name="Rule race archived rule",
        category_id=archived_category["id"],
        priority=51,
        counterparty_contains="archive shop",
    )
    typed_rule = _rule(
        client,
        headers,
        name="Rule race typed rule",
        category_id=typed_category["id"],
        priority=52,
        counterparty_contains="typed shop",
    )
    rule_changed_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop rulechange")
    )
    archived_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Archive Shop item")
    )
    typed_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Typed Shop item")
    )
    preview = _preview(client, headers, [rule_changed_id, archived_id, typed_id])
    items = _items(client, headers, preview["id"])
    assert [item["status"] for item in items] == ["matched"] * 3

    # Archive one target category and flip another's type: both must be category_changed.
    async def mutate_categories() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(Category)
                .where(Category.id == uuid.UUID(archived_category["id"]))
                .values(is_archived=True, version=2)
            )
            await session.execute(
                update(Category)
                .where(Category.id == uuid.UUID(typed_category["id"]))
                .values(category_type="income", version=2)
            )
            await session.commit()

    asyncio.run(mutate_categories())

    # A brand new higher-priority rule moves the rule-set revision.
    intruder = _rule(
        client,
        headers,
        name="Rule race intruder rule",
        category_id=intruder_category["id"],
        priority=1,
        counterparty_contains="apply shop",
    )
    assert intruder["id"] != rule["id"]
    assert archived_rule["id"] and typed_rule["id"]

    response = _apply(
        client, headers, preview["id"], [item["id"] for item in items], "key-rule-races"
    )
    assert response.status_code == 200, response.text
    statuses = [row["status"] for row in response.json()["results"]]
    # The rule-set revision moved, so every item is refused conservatively.
    assert statuses == ["rule_changed", "rule_changed", "rule_changed"]
    assert response.json()["results"][0]["error_code"] == "CATEGORIZATION_RULE_CHANGED"
    for transaction_id in (rule_changed_id, archived_id, typed_id):
        assert asyncio.run(_transaction_row(transaction_id)).category_id is None
        assert asyncio.run(_audit_count(transaction_id)) == 0


def test_category_archived_after_preview_without_a_rule_set_change(client: TestClient) -> None:
    identity, headers = _register(client, "Apply category race")
    account = _account(client, headers, "Category race card")
    category = _category(client, headers, "Category race target")
    _rule(
        client,
        headers,
        name="Category race rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop category")
    )
    preview = _preview(client, headers, [transaction_id])
    item_id = _items(client, headers, preview["id"])[0]["id"]

    # Archiving a category does not touch the rule set, so this proves the category guard itself.
    async def archive_category() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(Category)
                .where(Category.id == uuid.UUID(category["id"]))
                .values(is_archived=True, version=2)
            )
            await session.commit()

    asyncio.run(archive_category())

    response = _apply(client, headers, preview["id"], [item_id], "key-category")
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "category_changed"
    assert result["error_code"] == "CATEGORIZATION_CATEGORY_CHANGED"
    assert asyncio.run(_transaction_row(transaction_id)).category_id is None
    assert asyncio.run(_audit_count(transaction_id)) == 0


def test_month_close_after_preview_reports_closed_period(client: TestClient) -> None:
    identity, headers = _register(client, "Apply month close")
    account = _account(client, headers, "Close card")
    category = _category(client, headers, "Close target")
    _rule(
        client,
        headers,
        name="Close rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_id = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Apply Shop closed",
            occurred_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        )
    )
    preview = _preview(client, headers, [transaction_id])
    item_id = _items(client, headers, preview["id"])[0]["id"]
    asyncio.run(_close_through(identity, date(2026, 6, 30)))

    response = _apply(client, headers, preview["id"], [item_id], "key-closed")
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "closed_period"
    assert result["error_code"] == "MONTH_CLOSED"
    assert asyncio.run(_transaction_row(transaction_id)).category_id is None
    assert asyncio.run(_audit_count(transaction_id)) == 0


def test_ineligible_preview_statuses_are_never_attempted(client: TestClient) -> None:
    identity, headers = _register(client, "Apply ineligible")
    account = _account(client, headers, "Ineligible card")
    target_account = _account(client, headers, "Ineligible target account")
    category = _category(client, headers, "Ineligible target")
    _rule(
        client,
        headers,
        name="Ineligible rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transfer_id = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Apply Shop transfer",
            transaction_type="transfer",
            target_account_id=target_account["id"],
        )
    )
    unmatched_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Nothing matches this")
    )
    missing_id = str(uuid.uuid4())
    preview = _preview(client, headers, [transfer_id, unmatched_id, missing_id])
    items = _items(client, headers, preview["id"])
    assert [item["status"] for item in items] == ["transfer", "no_match", "not_found"]

    response = _apply(
        client, headers, preview["id"], [item["id"] for item in items], "key-ineligible"
    )
    assert response.status_code == 200, response.text
    assert [row["status"] for row in response.json()["results"]] == [
        "transfer",
        "no_match",
        "not_found",
    ]
    assert response.json()["summary"] == {
        "requested": 3,
        "applied": 0,
        "conflicts": 0,
        "not_applied": 3,
        "failed": 0,
    }
    for transaction_id in (transfer_id, unmatched_id):
        assert asyncio.run(_audit_count(transaction_id)) == 0


def test_two_operations_on_the_same_transaction_apply_exactly_once(client: TestClient) -> None:
    identity, headers = _register(client, "Apply overlap")
    account = _account(client, headers, "Overlap card")
    category = _category(client, headers, "Overlap target")
    _rule(
        client,
        headers,
        name="Overlap rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop overlap")
    )
    first_preview = _preview(client, headers, [transaction_id])
    second_preview = _preview(client, headers, [transaction_id])
    first_item = _items(client, headers, first_preview["id"])[0]["id"]
    second_item = _items(client, headers, second_preview["id"])[0]["id"]

    first = _apply(client, headers, first_preview["id"], [first_item], "key-overlap-1")
    second = _apply(client, headers, second_preview["id"], [second_item], "key-overlap-2")
    assert first.status_code == second.status_code == 200
    statuses = {first.json()["results"][0]["status"], second.json()["results"][0]["status"]}
    # Documented precedence: transaction version identity is proved before state guards, so the
    # loser reports the stale version rather than the category it did not write.
    assert statuses == {"applied", "transaction_changed"}
    assert asyncio.run(_transaction_row(transaction_id)).version == 2
    assert asyncio.run(_audit_count(transaction_id)) == 1
    assert identity is not None


def test_concurrent_requests_sharing_a_key_create_one_operation(client: TestClient) -> None:
    identity, _headers, _account, _category, _rule_row, transaction_id, preview, items = (
        _matched_scenario(client, "Apply concurrent key")
    )
    item_id = uuid.UUID(items[0]["id"])

    async def run_both() -> list[apply_service.BulkApplyOutcome]:
        async def one() -> apply_service.BulkApplyOutcome:
            async with AsyncSessionFactory() as session:
                return await apply_service.apply_preview_items(
                    session,
                    await _context_for(identity, session),
                    uuid.UUID(preview["id"]),
                    CategorizationApplyRequest(item_ids=[item_id]),
                    "key-concurrent",
                )

        return list(await asyncio.gather(one(), one()))

    outcomes = asyncio.run(run_both())
    assert outcomes[0].operation_id == outcomes[1].operation_id
    assert {outcome.results[0].status for outcome in outcomes} == {"applied"}
    assert asyncio.run(_transaction_row(transaction_id)).version == 2
    assert asyncio.run(_audit_count(transaction_id)) == 1
    assert len(asyncio.run(_results_for(str(outcomes[0].operation_id)))) == 1


def test_concurrent_same_key_requests_with_different_orders_converge_stably(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, _headers = _register(client, "Apply concurrent reordered key")
    account = _account(client, _headers, "Concurrent reordered card")
    category = _category(client, _headers, "Concurrent reordered target")
    _rule(
        client,
        _headers,
        name="Concurrent reordered rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_ids = [
        asyncio.run(
            _insert_transaction(
                identity,
                account["id"],
                counterparty=f"Apply Shop concurrent reordered {index}",
            )
        )
        for index in range(3)
    ]
    preview = _preview(client, _headers, transaction_ids)
    canonical_item_ids = sorted(
        uuid.UUID(item["id"]) for item in _items(client, _headers, preview["id"])
    )
    reversed_item_ids = list(reversed(canonical_item_ids))

    original_process_item = apply_service._process_item
    entered = 0
    both_entered = asyncio.Event()

    async def synchronize_first_items(*args: object, **kwargs: object):
        nonlocal entered
        entered += 1
        if entered <= 2:
            if entered == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5)
        return await original_process_item(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(apply_service, "_process_item", synchronize_first_items)

    async def run_both() -> list[apply_service.BulkApplyOutcome]:
        async def one(item_ids: list[uuid.UUID]) -> apply_service.BulkApplyOutcome:
            async with AsyncSessionFactory() as session:
                return await apply_service.apply_preview_items(
                    session,
                    await _context_for(identity, session),
                    uuid.UUID(preview["id"]),
                    CategorizationApplyRequest(item_ids=item_ids),
                    "key-concurrent-reordered",
                )

        return list(
            await asyncio.gather(
                one(canonical_item_ids),
                one(reversed_item_ids),
            )
        )

    outcomes = asyncio.run(run_both())
    assert outcomes[0].operation_id == outcomes[1].operation_id
    assert [row.item_id for row in outcomes[0].results] == canonical_item_ids
    assert [row.item_id for row in outcomes[1].results] == reversed_item_ids
    assert {row.status for outcome in outcomes for row in outcome.results} == {"applied"}

    persisted = asyncio.run(_results_for(str(outcomes[0].operation_id)))
    assert [(row.item_id, row.sequence) for row in persisted] == [
        (item_id, sequence) for sequence, item_id in enumerate(canonical_item_ids)
    ]
    for transaction_id in transaction_ids:
        assert asyncio.run(_transaction_row(transaction_id)).version == 2
        assert asyncio.run(_audit_count(transaction_id)) == 1
        assert asyncio.run(_outbox_count(transaction_id)) == 0


def test_rule_mutation_waits_for_a_bulk_apply_holding_the_shared_rule_set_lock(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers, _account, category, _rule_row, transaction_id, preview, items = (
        _matched_scenario(client, "Apply barrier")
    )
    intruder_category = _category(client, headers, "Barrier intruder")
    item_id = uuid.UUID(items[0]["id"])

    lock_held = asyncio.Event()
    allow_finish = asyncio.Event()
    original_execute = apply_service.executor.execute_apply

    async def paused_execute(session, context, expectation, *, commit):
        outcome = await original_execute(session, context, expectation, commit=commit)
        lock_held.set()
        await allow_finish.wait()
        return outcome

    monkeypatch.setattr(apply_service.executor, "execute_apply", paused_execute)

    async def run_barrier() -> tuple[str, bool]:
        async with AsyncSessionFactory() as apply_session:
            apply_task = asyncio.create_task(
                apply_service.apply_preview_items(
                    apply_session,
                    await _context_for(identity, apply_session),
                    uuid.UUID(preview["id"]),
                    CategorizationApplyRequest(item_ids=[item_id]),
                    "key-barrier",
                )
            )
            await lock_held.wait()
            async with AsyncSessionFactory() as mutation_session:
                from app.schemas.categorization_rules import CategorizationRuleCreate

                mutation_task = asyncio.create_task(
                    rule_service.create_rule(
                        mutation_session,
                        await _context_for(identity, mutation_session),
                        CategorizationRuleCreate(
                            name="Barrier intruder rule",
                            priority=1,
                            category_id=uuid.UUID(intruder_category["id"]),
                            counterparty_contains="apply shop",
                        ),
                    )
                )
                await asyncio.sleep(0.5)
                blocked = not mutation_task.done()
                allow_finish.set()
                outcome = await apply_task
                await mutation_task
            return outcome.results[0].status, blocked

    status, blocked = asyncio.run(run_barrier())
    assert blocked is True
    assert status == "applied"
    committed = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
    assert committed.json()["category"]["id"] == category["id"]
    assert asyncio.run(_audit_count(transaction_id)) == 1
