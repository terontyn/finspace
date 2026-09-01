"""Stage B: successful categorization mutations carry durable causal metadata.

Every assertion here is about the audit row that accompanies the *actual* transaction mutation.
Non-mutating outcomes keep their existing persisted apply-result evidence and must not gain an
audit row.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.categorization_apply_operations import CategorizationApplyResult
from app.db.models.categorization_rules import CategorizationRule
from app.db.models.transactions import FinancialTransaction
from app.db.session import AsyncSessionFactory
from app.services.audit import (
    CAUSE_CATEGORIZATION_RULE,
    CAUSE_SOURCE_BULK_APPLY,
    CAUSE_SOURCE_SINGLE_APPLY,
)
from tests.test_categorization_bulk_apply import (
    _account,
    _apply,
    _category,
    _insert_transaction,
    _items,
    _matched_scenario,
    _preview,
    _register,
    _rule,
)


@pytest.fixture(autouse=True)
def _configure_causation_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """An autouse fixture only covers its own module, so this one is declared here too."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


async def _audit_rows(transaction_id: str) -> list[AuditLog]:
    """Every mutation audit row for one transaction, oldest first."""
    async with AsyncSessionFactory() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "transaction",
                        AuditLog.entity_id == uuid.UUID(transaction_id),
                        AuditLog.action == "update",
                    )
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            ).all()
        )


def _sole_cause(transaction_id: str) -> AuditLog:
    rows = asyncio.run(_audit_rows(transaction_id))
    assert len(rows) == 1, f"expected exactly one mutation audit row, got {len(rows)}"
    return rows[0]


def test_single_apply_writes_one_causal_audit_row(client: TestClient) -> None:
    identity, headers = _register(client, "Single cause")
    account = _account(client, headers, "Single card")
    category = _category(client, headers, "Single target")
    rule = _rule(
        client,
        headers,
        name="Single rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="apply shop",
    )
    transaction_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Apply Shop statement")
    )
    transaction = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers).json()

    response = client.post(
        f"/api/v1/transactions/{transaction_id}/apply-categorization",
        headers=headers,
        json={"version": transaction["version"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True

    entry = _sole_cause(transaction_id)
    assert entry.cause_type == CAUSE_CATEGORIZATION_RULE
    assert entry.cause_id == uuid.UUID(rule["id"])
    assert entry.cause_metadata == {"source": CAUSE_SOURCE_SINGLE_APPLY}
    # The channel column keeps its own meaning and is not repurposed for the cause.
    assert entry.source == "api"


def test_bulk_apply_writes_one_causal_audit_row_per_applied_item(client: TestClient) -> None:
    _, headers, _, _, rule, transaction_id, preview, items = _matched_scenario(client, "Bulk cause")
    response = _apply(client, headers, preview["id"], [items[0]["id"]], "bulk-cause-1")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["results"][0]["status"] == "applied"
    operation_id = payload["operation_id"]

    entry = _sole_cause(transaction_id)
    assert entry.cause_type == CAUSE_CATEGORIZATION_RULE
    assert entry.cause_id == uuid.UUID(rule["id"])
    assert entry.cause_metadata == {
        "source": CAUSE_SOURCE_BULK_APPLY,
        "preview_id": preview["id"],
        "operation_id": operation_id,
    }


def test_same_key_replay_adds_no_second_audit_row(client: TestClient) -> None:
    _, headers, _, _, rule, transaction_id, preview, items = _matched_scenario(
        client, "Replay cause"
    )
    first = _apply(client, headers, preview["id"], [items[0]["id"]], "replay-cause")
    assert first.status_code == 200, first.text
    before = _sole_cause(transaction_id)

    second = _apply(client, headers, preview["id"], [items[0]["id"]], "replay-cause")
    assert second.status_code == 200, second.text
    assert second.json()["results"][0]["status"] == "applied"

    after = _sole_cause(transaction_id)
    assert after.id == before.id
    assert after.cause_id == uuid.UUID(rule["id"])
    assert after.cause_metadata == before.cause_metadata


def test_interrupted_recovery_does_not_duplicate_causal_audit(client: TestClient) -> None:
    """One item already applied and recorded, a second resumed under the same operation."""
    identity, headers = _register(client, "Resume cause")
    account = _account(client, headers, "Resume card")
    category = _category(client, headers, "Resume target")
    rule = _rule(
        client,
        headers,
        name="Resume rule",
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
    item_ids = [item["id"] for item in items]

    applied = _apply(client, headers, preview["id"], item_ids, "resume-cause")
    assert applied.status_code == 200, applied.text
    operation_id = applied.json()["operation_id"]

    first_audit = _sole_cause(first_id)
    second_audit = _sole_cause(second_id)

    replay = _apply(client, headers, preview["id"], item_ids, "resume-cause")
    assert replay.status_code == 200, replay.text
    assert {item["status"] for item in replay.json()["results"]} == {"applied"}

    # Neither item gained a second audit row, and the causal evidence is unchanged.
    assert _sole_cause(first_id).id == first_audit.id
    assert _sole_cause(second_id).id == second_audit.id
    for entry in (first_audit, second_audit):
        assert entry.cause_id == uuid.UUID(rule["id"])
        assert entry.cause_metadata["source"] == CAUSE_SOURCE_BULK_APPLY
        assert entry.cause_metadata["operation_id"] == operation_id


def test_overlapping_operations_leave_exactly_one_causal_audit(client: TestClient) -> None:
    """Two operations over the same item: only the winner mutates, so only it leaves a cause."""
    _, headers, _, _, _, transaction_id, preview, items = _matched_scenario(client, "Overlap cause")
    winner = _apply(client, headers, preview["id"], [items[0]["id"]], "overlap-a")
    assert winner.status_code == 200, winner.text
    assert winner.json()["results"][0]["status"] == "applied"

    loser = _apply(client, headers, preview["id"], [items[0]["id"]], "overlap-b")
    assert loser.status_code == 200, loser.text
    # The second operation cannot mutate an already categorized transaction.
    assert loser.json()["results"][0]["status"] != "applied"

    entry = _sole_cause(transaction_id)
    assert entry.cause_metadata["operation_id"] == winner.json()["operation_id"]


def test_causal_audit_survives_rule_archive_and_hard_delete(client: TestClient) -> None:
    _, headers, _, _, rule, transaction_id, preview, items = _matched_scenario(
        client, "Lifecycle cause"
    )
    applied = _apply(client, headers, preview["id"], [items[0]["id"]], "lifecycle-cause")
    assert applied.status_code == 200, applied.text
    rule_id = uuid.UUID(rule["id"])

    async def _archive_then_delete() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(CategorizationRule)
                .where(CategorizationRule.id == rule_id)
                .values(is_active=False)
            )
            await session.commit()
        async with AsyncSessionFactory() as session:
            # Hard delete: no foreign key may block this or cascade the audit away.
            await session.execute(
                delete(CategorizationApplyResult).where(
                    CategorizationApplyResult.transaction_id == uuid.UUID(transaction_id)
                )
            )
            await session.execute(
                delete(CategorizationRule).where(CategorizationRule.id == rule_id)
            )
            await session.commit()

    asyncio.run(_archive_then_delete())

    entry = _sole_cause(transaction_id)
    assert entry.cause_type == CAUSE_CATEGORIZATION_RULE
    assert entry.cause_id == rule_id
    assert entry.cause_metadata["source"] == CAUSE_SOURCE_BULK_APPLY


def test_manual_transaction_update_records_audit_without_cause(client: TestClient) -> None:
    identity, headers = _register(client, "Manual cause")
    account = _account(client, headers, "Manual card")
    category = _category(client, headers, "Manual target")
    transaction_id = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Manual counterparty",
            category_id=category["id"],
        )
    )
    current = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers).json()

    response = client.patch(
        f"/api/v1/transactions/{transaction_id}",
        headers=headers,
        json={"version": current["version"], "comment": "manual edit"},
    )
    assert response.status_code == 200, response.text

    entry = _sole_cause(transaction_id)
    assert entry.cause_type is None
    assert entry.cause_id is None
    assert entry.cause_metadata is None
    assert entry.source == "api"


def test_rollback_leaves_neither_mutation_nor_causal_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, headers, _, _, _, transaction_id, preview, items = _matched_scenario(
        client, "Rollback cause"
    )

    from app.services import categorization_apply as apply_service

    original = apply_service._persist
    calls = {"n": 0}

    async def _boom_once(*args: object, **kwargs: object):
        """Fail the first persist only.

        The mutation and its causal audit row are already written at this point; the failure
        isolation path must still be able to record the terminal result, so later calls run for
        real. Patching every call would break ``_record_failure`` itself and mask the rollback.
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("forced failure before commit")
        return await original(*args, **kwargs)

    monkeypatch.setattr(apply_service, "_persist", _boom_once)
    response = _apply(client, headers, preview["id"], [items[0]["id"]], "rollback-cause")
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["status"] == "failed"

    async def _state() -> tuple[int, uuid.UUID | None]:
        async with AsyncSessionFactory() as session:
            rows = int(
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
            category_id = await session.scalar(
                select(FinancialTransaction.category_id).where(
                    FinancialTransaction.id == uuid.UUID(transaction_id)
                )
            )
            return rows, category_id

    audit_rows, category_id = asyncio.run(_state())
    assert audit_rows == 0, "rolled-back mutation must leave no causal audit"
    assert category_id is None, "rolled-back mutation must leave the transaction uncategorized"


def test_audit_api_exposes_cause_fields(client: TestClient) -> None:
    _, headers, _, _, rule, transaction_id, preview, items = _matched_scenario(
        client, "Exposed cause"
    )
    applied = _apply(client, headers, preview["id"], [items[0]["id"]], "exposed-cause")
    assert applied.status_code == 200, applied.text

    response = client.get(
        f"/api/v1/audit?entity_type=transaction&entity_id={transaction_id}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    entries = [item for item in response.json()["items"] if item["action"] == "update"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["cause_type"] == CAUSE_CATEGORIZATION_RULE
    assert entry["cause_id"] == rule["id"]
    assert entry["cause_metadata"]["source"] == CAUSE_SOURCE_BULK_APPLY
    assert entry["cause_metadata"]["preview_id"] == preview["id"]
