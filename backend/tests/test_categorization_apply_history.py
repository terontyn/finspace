import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text, update

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.categories import Category
from app.db.models.categorization_apply_operations import (
    APPLY_RESULT_STATUSES,
    CategorizationApplyOperation,
    CategorizationApplyResult,
)
from app.db.models.categorization_previews import CategorizationPreview, CategorizationPreviewItem
from app.db.models.categorization_rules import CategorizationRule
from app.db.models.google_sync import SyncOutbox
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, WorkspaceMember
from app.db.session import AsyncSessionFactory, engine
from app.services import categorization_matcher
from tests.test_categorization_bulk_apply import (
    PREVIEWS,
    _account,
    _category,
    _insert_transaction,
    _register,
    _rule,
)

HISTORY = "/api/v1/categorization-apply-operations"


@pytest.fixture(autouse=True)
def _configure_history_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


async def _add_member(identity: dict, workspace_id: str, role: str) -> dict[str, str]:
    async with AsyncSessionFactory() as session:
        session.add(
            WorkspaceMember(
                workspace_id=uuid.UUID(workspace_id),
                user_id=uuid.UUID(identity["user"]["id"]),
                role=role,
            )
        )
        await session.commit()
    return {
        "Authorization": f"Bearer {identity['access_token']}",
        "X-Workspace-ID": workspace_id,
    }


async def _set_member_role(identity: dict, workspace_id: str, role: str) -> None:
    async with AsyncSessionFactory() as session:
        await session.execute(
            update(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == uuid.UUID(workspace_id),
                WorkspaceMember.user_id == uuid.UUID(identity["user"]["id"]),
            )
            .values(role=role)
        )
        await session.commit()


async def _insert_operation(
    identity: dict,
    *,
    operation_id: uuid.UUID | None = None,
    created_at: datetime,
    requested_count: int,
    statuses: list[tuple[str, uuid.UUID | None]],
    status: str = "completed",
    preview_id: uuid.UUID | None = None,
) -> uuid.UUID:
    operation_id = operation_id or uuid.uuid4()
    async with AsyncSessionFactory() as session:
        operation = CategorizationApplyOperation(
            id=operation_id,
            workspace_id=uuid.UUID(identity["workspace"]["id"]),
            preview_id=preview_id or uuid.uuid4(),
            actor_user_id=uuid.UUID(identity["user"]["id"]),
            idempotency_key=f"history-{operation_id}",
            request_hash="a" * 64,
            status=status,
            requested_count=requested_count,
            created_at=created_at,
            completed_at=created_at + timedelta(seconds=1) if status == "completed" else None,
        )
        session.add(operation)
        session.add_all(
            [
                CategorizationApplyResult(
                    operation_id=operation_id,
                    item_id=uuid.uuid4(),
                    sequence=sequence,
                    transaction_id=transaction_id,
                    status=result_status,
                    error_code="TEST_FAILURE" if result_status == "failed" else None,
                    expected_version=3 if result_status == "transaction_changed" else None,
                    current_version=4 if result_status == "transaction_changed" else None,
                    created_at=created_at + timedelta(milliseconds=sequence),
                )
                for sequence, (result_status, transaction_id) in enumerate(statuses)
            ]
        )
        await session.commit()
    return operation_id


def _assert_private_fields_absent(payload: object) -> None:
    encoded = str(payload)
    for forbidden in ("idempotency_key", "request_hash", "preview_id", "item_id"):
        assert forbidden not in encoded


def test_history_contract_permissions_order_counts_pagination_and_no_n_plus_one(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "History contract")
    workspace_id = identity["workspace"]["id"]
    base = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    lower_tie = uuid.UUID(int=101)
    higher_tie = uuid.UUID(int=102)
    all_statuses = [(status, uuid.uuid4()) for status in APPLY_RESULT_STATUSES]
    newest_id = asyncio.run(
        _insert_operation(
            identity,
            created_at=base + timedelta(minutes=2),
            requested_count=len(all_statuses),
            statuses=all_statuses,
        )
    )
    asyncio.run(
        _insert_operation(
            identity,
            operation_id=lower_tie,
            created_at=base + timedelta(minutes=1),
            requested_count=1,
            statuses=[("applied", uuid.uuid4())],
        )
    )
    asyncio.run(
        _insert_operation(
            identity,
            operation_id=higher_tie,
            created_at=base + timedelta(minutes=1),
            requested_count=5,
            statuses=[("failed", uuid.uuid4()), ("no_match", uuid.uuid4())],
            status="in_progress",
        )
    )
    for index in range(20):
        asyncio.run(
            _insert_operation(
                identity,
                created_at=base - timedelta(minutes=index + 1),
                requested_count=1,
                statuses=[("applied", uuid.uuid4())],
            )
        )

    result_queries: list[str] = []

    def capture_result_query(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if "from categorization_apply_results" in normalized:
            result_queries.append(normalized)

    event.listen(engine.sync_engine, "before_cursor_execute", capture_result_query)
    try:
        response = client.get(HISTORY, headers=headers)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_result_query)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["page"] == {"limit": 20, "offset": 0, "total": 23}
    assert len(payload["items"]) == 20
    assert payload["items"][0]["id"] == str(newest_id)
    assert payload["items"][1]["id"] == str(higher_tie)
    assert payload["items"][2]["id"] == str(lower_tie)
    assert len(result_queries) == 1
    assert "group by" in result_queries[0]

    summary = payload["items"][0]
    assert summary["result_count"] == len(APPLY_RESULT_STATUSES)
    assert summary["counts"] == {status: 1 for status in APPLY_RESULT_STATUSES}
    assert summary["actor"]["display_name"] == identity["user"]["display_name"]
    partial = payload["items"][1]
    assert partial["status"] == "in_progress"
    assert partial["requested_count"] == 5
    assert partial["result_count"] == 2
    _assert_private_fields_absent(payload)

    second = client.get(f"{HISTORY}?limit=20&offset=20", headers=headers)
    assert second.status_code == 200
    assert len(second.json()["items"]) == 3
    assert client.get(f"{HISTORY}?limit=101", headers=headers).status_code == 422

    for role in ("viewer", "editor", "owner"):
        asyncio.run(_set_member_role(identity, workspace_id, role))
        role_response = client.get(f"{HISTORY}?limit=1", headers=headers)
        assert role_response.status_code == 200
        assert role_response.json()["items"][0]["id"] == str(newest_id)


def test_history_detail_is_scoped_ordered_paginated_private_and_actor_is_nullable(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "History detail")
    other, other_headers = _register(client, "History foreign")
    operation_id = asyncio.run(
        _insert_operation(
            identity,
            created_at=datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
            requested_count=3,
            statuses=[
                ("failed", uuid.uuid4()),
                ("transaction_changed", uuid.uuid4()),
                ("not_found", None),
            ],
        )
    )
    detail = client.get(f"{HISTORY}/{operation_id}?limit=2&offset=1", headers=headers)
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["page"] == {"limit": 2, "offset": 1, "total": 3}
    assert [row["sequence"] for row in payload["results"]] == [1, 2]
    assert payload["results"][0]["expected_version"] == 3
    assert payload["results"][0]["current_version"] == 4
    _assert_private_fields_absent(payload)
    assert client.get(f"{HISTORY}/{operation_id}?limit=101", headers=headers).status_code == 422

    missing = client.get(f"{HISTORY}/{uuid.uuid4()}", headers=headers)
    foreign = client.get(f"{HISTORY}/{operation_id}", headers=other_headers)
    for response in (missing, foreign):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CATEGORIZATION_APPLY_OPERATION_NOT_FOUND"

    async def update_actor() -> dict[str, str]:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(User)
                .where(User.id == uuid.UUID(identity["user"]["id"]))
                .values(display_name="Current actor name")
            )
            await session.commit()
        return await _add_member(other, identity["workspace"]["id"], "viewer")

    shared_headers = asyncio.run(update_actor())
    current = client.get(f"{HISTORY}/{operation_id}", headers=shared_headers)
    assert current.json()["actor"]["display_name"] == "Current actor name"

    async def soft_delete_actor() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(User)
                .where(User.id == uuid.UUID(identity["user"]["id"]))
                .values(deleted_at=datetime.now(UTC))
            )
            await session.commit()

    asyncio.run(soft_delete_actor())
    unavailable = client.get(f"{HISTORY}/{operation_id}", headers=shared_headers)
    assert unavailable.status_code == 200
    assert unavailable.json()["actor"] == {
        "actor_user_id": identity["user"]["id"],
        "display_name": None,
    }


def test_history_survives_preview_pruning_and_get_is_read_only(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "History pruning")
    created_at = datetime.now(UTC) - timedelta(days=2)
    preview_id = uuid.uuid4()
    transaction_id = uuid.uuid4()

    async def seed_and_snapshot() -> tuple[uuid.UUID, dict[str, int]]:
        async with AsyncSessionFactory() as session:
            preview = CategorizationPreview(
                id=preview_id,
                workspace_id=uuid.UUID(identity["workspace"]["id"]),
                created_by=uuid.UUID(identity["user"]["id"]),
                rule_set_version=1,
                selection_mode="ids",
                selection={"mode": "ids", "transaction_ids": [str(transaction_id)]},
                selected_count=1,
                matched_count=0,
                no_match_count=1,
                transfer_count=0,
                already_categorized_count=0,
                split_count=0,
                reconciled_count=0,
                closed_period_count=0,
                not_found_count=0,
                created_at=created_at,
                expires_at=created_at + timedelta(hours=1),
            )
            session.add(preview)
            session.add(
                CategorizationPreviewItem(
                    preview_id=preview_id,
                    sequence=0,
                    transaction_id=transaction_id,
                    transaction_version=None,
                    status="no_match",
                )
            )
            await session.commit()
        operation_id = await _insert_operation(
            identity,
            created_at=created_at,
            requested_count=1,
            statuses=[("no_match", transaction_id)],
            preview_id=preview_id,
        )
        async with AsyncSessionFactory() as session:
            await session.execute(
                delete(CategorizationPreview).where(CategorizationPreview.id == preview_id)
            )
            await session.commit()
            models = (FinancialTransaction, Category, CategorizationRule, AuditLog, SyncOutbox)
            snapshot = {
                model.__tablename__: int(
                    await session.scalar(select(func.count()).select_from(model)) or 0
                )
                for model in models
            }
            assert await session.get(CategorizationPreview, preview_id) is None
        return operation_id, snapshot

    operation_id, before = asyncio.run(seed_and_snapshot())

    async def matcher_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("history GET must not invoke categorization matcher")

    monkeypatch.setattr(categorization_matcher, "prepare_rule_set", matcher_must_not_run)
    listed = client.get(HISTORY, headers=headers)
    detail = client.get(f"{HISTORY}/{operation_id}", headers=headers)
    assert listed.status_code == 200
    assert str(operation_id) in [row["id"] for row in listed.json()["items"]]
    assert detail.status_code == 200
    assert detail.json()["results"][0]["status"] == "no_match"

    async def snapshot_after() -> dict[str, int]:
        async with AsyncSessionFactory() as session:
            models = (FinancialTransaction, Category, CategorizationRule, AuditLog, SyncOutbox)
            return {
                model.__tablename__: int(
                    await session.scalar(select(func.count()).select_from(model)) or 0
                )
                for model in models
            }

    assert asyncio.run(snapshot_after()) == before
    assert client.get(f"{PREVIEWS}/{preview_id}", headers=headers).status_code == 404


def test_re_review_uses_existing_preview_contract_without_original_preview_or_apply(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "History re-review")
    account = _account(client, headers, "History card")
    category = _category(client, headers, "Current category")
    current_rule = _rule(
        client,
        headers,
        name="Current rule",
        category_id=category["id"],
        priority=1,
        counterparty_contains="history current",
    )
    eligible_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="History Current merchant")
    )
    unrelated_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="History unrelated")
    )
    missing_id = str(uuid.uuid4())
    operation_id = asyncio.run(
        _insert_operation(
            identity,
            created_at=datetime.now(UTC),
            requested_count=3,
            statuses=[
                ("rule_changed", uuid.UUID(eligible_id)),
                ("applied", uuid.UUID(unrelated_id)),
                ("failed", uuid.UUID(missing_id)),
            ],
        )
    )

    async def original_preview_is_absent() -> bool:
        async with AsyncSessionFactory() as session:
            operation = await session.get(CategorizationApplyOperation, operation_id)
            assert operation is not None
            return await session.get(CategorizationPreview, operation.preview_id) is None

    assert asyncio.run(original_preview_is_absent()) is True

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

    before = asyncio.run(operation_count())
    detail = client.get(f"{HISTORY}/{operation_id}", headers=headers).json()
    eligible = [
        row["transaction_id"]
        for row in detail["results"]
        if row["status"]
        in {"transaction_changed", "rule_changed", "category_changed", "no_match", "failed"}
        and row["transaction_id"] is not None
    ]
    assert eligible == [eligible_id, missing_id]

    preview = client.post(
        PREVIEWS,
        headers=headers,
        json={"selection": {"mode": "ids", "transaction_ids": eligible}},
    )
    assert preview.status_code == 201, preview.text
    items_response = client.get(
        f"{PREVIEWS}/{preview.json()['id']}/items?limit=100", headers=headers
    )
    items = items_response.json()["items"]
    assert [row["transaction_id"] for row in items] == [eligible_id, missing_id]
    matched = items[0]
    assert matched["status"] == "matched"
    assert matched["rule_id"] == current_rule["id"]
    assert matched["rule_name"] == "Current rule"
    assert matched["category_id"] == category["id"]
    assert matched["transaction_version"] == 1
    assert items[1]["status"] == "not_found"
    assert unrelated_id not in [row["transaction_id"] for row in items]
    assert "preview_id" not in detail
    assert asyncio.run(operation_count()) == before
    assert (
        client.post(
            PREVIEWS,
            headers=headers,
            json={
                "selection": {
                    "mode": "ids",
                    "transaction_ids": [str(uuid.uuid4()) for _ in range(501)],
                }
            },
        ).status_code
        == 422
    )


def test_history_migration_preserves_existing_apply_ledger_across_exact_boundary(
    client: TestClient,
) -> None:
    identity, _headers = _register(client, "History migration")
    operation_id = asyncio.run(
        _insert_operation(
            identity,
            created_at=datetime.now(UTC),
            requested_count=1,
            statuses=[("failed", uuid.uuid4())],
        )
    )

    async def index_and_row() -> tuple[str | None, int]:
        async with AsyncSessionFactory() as session:
            index_name = await session.scalar(
                text(
                    "SELECT to_regclass("
                    "'public.ix_categorization_apply_operations_workspace_created')::text"
                )
            )
            row_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(CategorizationApplyOperation)
                    .where(CategorizationApplyOperation.id == operation_id)
                )
                or 0
            )
            return index_name, row_count

    assert asyncio.run(index_and_row()) == (
        "ix_categorization_apply_operations_workspace_created",
        1,
    )
    cycle_environment = {**os.environ, "MIGRATION_TEST_CYCLE": "true"}
    asyncio.run(engine.dispose())
    subprocess.run(
        ["alembic", "downgrade", "0016_audit_cause"],
        check=True,
        env=cycle_environment,
    )
    assert asyncio.run(index_and_row()) == (None, 1)
    asyncio.run(engine.dispose())
    subprocess.run(["alembic", "upgrade", "head"], check=True, env=cycle_environment)
    assert asyncio.run(index_and_row()) == (
        "ix_categorization_apply_operations_workspace_created",
        1,
    )
