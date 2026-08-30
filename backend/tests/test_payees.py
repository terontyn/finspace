import asyncio
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.audit import AuditLog
from app.db.models.automations import MonthCloseControl, RecurringRule
from app.db.models.payees import PayeeAlias
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace
from app.db.session import AsyncSessionFactory, engine
from app.dependencies.context import RequestContext
from app.schemas.payees import PayeeAliasCreate, PayeeUpdate
from app.services import payees as payee_service
from app.services.payees import alias_identity, normalize_alias
from app.services.sync_hash import row_hash
from app.services.sync_payload import transaction_payload

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _configure_payee_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _register(client: TestClient, label: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"payees-{uuid.uuid4()}@example.com",
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


def _references(client: TestClient, headers: dict[str, str], label: str) -> tuple[dict, dict]:
    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": f"{label} card",
            "account_type": "debit_card",
            "currency": "RUB",
            "opening_balance": "100000",
            "opening_balance_at": "2026-07-01T00:00:00Z",
        },
    )
    category = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": f"{label} groceries", "category_type": "expense"},
    )
    assert account.status_code == category.status_code == 201
    return account.json(), category.json()


def _payee(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post(
        "/api/v1/payees",
        headers=headers,
        json={"name": name, "notes": "Test Payee"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _transaction(
    client: TestClient,
    headers: dict[str, str],
    account_id: str,
    category_id: str,
    *,
    payee_id: str | None,
    counterparty: str = "Original statement text",
    occurred_at: str = "2026-08-15T12:00:00Z",
) -> dict:
    response = client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": occurred_at,
            "transaction_type": "expense",
            "amount": "1250.25",
            "currency": "RUB",
            "account_id": account_id,
            "category_id": category_id,
            "payee_id": payee_id,
            "counterparty": counterparty,
            "status": "confirmed",
            "source": "manual",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _recurring_payload(
    account_id: str,
    category_id: str,
    *,
    payee_id: str | None,
    name: str = "Payee recurring",
) -> dict[str, object]:
    return {
        "name": name,
        "rule_type": "expense",
        "schedule_rrule": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0",
        "timezone": "Asia/Yekaterinburg",
        "transaction_type": "expense",
        "amount": "500.00",
        "currency": "RUB",
        "account_id": account_id,
        "category_id": category_id,
        "payee_id": payee_id,
        "counterparty": "Recurring statement snapshot",
        "description": "Template description",
        "comment": "Template comment",
        "creation_mode": "draft",
    }


def test_normalization_is_unicode_conservative() -> None:
    assert normalize_alias("  IKEA\u2003\u2003Екатеринбург ") == normalize_alias(  # noqa: RUF001
        "ikea Екатеринбург"
    )
    assert normalize_alias("Ａｍａｚｏｎ 1234") == normalize_alias("Amazon 1234")  # noqa: RUF001
    assert normalize_alias("ООО Ромашка") != normalize_alias("Ромашка")  # noqa: RUF001
    assert normalize_alias("Amazon 1234") != normalize_alias("Amazon")
    assert normalize_alias("A-B") != normalize_alias("AB")
    normalized, digest = alias_identity("  Étoile  ")
    assert normalized == "étoile"
    assert len(digest) == 64


def test_payee_crud_alias_lifecycle_and_rename(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Payee lifecycle")
    created = _payee(client, headers, "  Магазин Étoile  ")
    assert created["name"] == "Магазин Étoile"
    assert len(created["aliases"]) == 1
    assert created["aliases"][0]["is_primary"] is True

    normal = _payee(client, headers, "Normal rename source")
    normal_renamed = client.patch(
        f"/api/v1/payees/{normal['id']}",
        headers=headers,
        json={"version": normal["version"], "name": "Normal rename destination"},
    )
    assert normal_renamed.status_code == 200, normal_renamed.text
    assert sum(item["is_primary"] for item in normal_renamed.json()["aliases"]) == 1
    assert {item["alias"] for item in normal_renamed.json()["aliases"]} == {
        "Normal rename source",
        "Normal rename destination",
    }

    duplicate = client.post(
        "/api/v1/payees",
        headers=headers,
        json={"name": "магазин\u2003ÉTOILE"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PAYEE_ALIAS_CONFLICT"

    added = client.post(
        f"/api/v1/payees/{created['id']}/aliases",
        headers=headers,
        json={"version": created["version"], "alias": "Etoile Market"},
    )
    assert added.status_code == 201, added.text
    payee = added.json()
    assert payee["version"] == 2
    primary = next(item for item in payee["aliases"] if item["is_primary"])
    delete_primary = client.delete(
        f"/api/v1/payees/{payee['id']}/aliases/{primary['id']}?version={payee['version']}",
        headers=headers,
    )
    assert delete_primary.status_code == 409
    assert delete_primary.json()["error"]["code"] == "PAYEE_PRIMARY_ALIAS_REQUIRED"

    renamed = client.patch(
        f"/api/v1/payees/{payee['id']}",
        headers=headers,
        json={"version": payee["version"], "name": "ETOILE MARKET"},
    )
    assert renamed.status_code == 200, renamed.text
    payee = renamed.json()
    assert payee["name"] == "ETOILE MARKET"
    assert sum(item["is_primary"] for item in payee["aliases"]) == 1
    assert {item["alias"] for item in payee["aliases"]} >= {
        "Магазин Étoile",
        "ETOILE MARKET",
    }

    old_alias = next(item for item in payee["aliases"] if item["alias"] == "Магазин Étoile")
    deleted_alias = client.delete(
        f"/api/v1/payees/{payee['id']}/aliases/{old_alias['id']}?version={payee['version']}",
        headers=headers,
    )
    assert deleted_alias.status_code == 200, deleted_alias.text
    payee = deleted_alias.json()
    assert next(item for item in payee["aliases"] if item["id"] == old_alias["id"])["deleted_at"]

    restored = client.post(
        f"/api/v1/payees/{payee['id']}/aliases/{old_alias['id']}/restore",
        headers=headers,
        json={"version": payee["version"]},
    )
    assert restored.status_code == 200, restored.text
    payee = restored.json()
    assert (
        next(item for item in payee["aliases"] if item["id"] == old_alias["id"])["deleted_at"]
        is None
    )

    alias_to_restore = client.post(
        f"/api/v1/payees/{payee['id']}/aliases",
        headers=headers,
        json={"version": payee["version"], "alias": "Deleted destination"},
    ).json()
    destination = next(
        item for item in alias_to_restore["aliases"] if item["alias"] == "Deleted destination"
    )
    after_delete = client.delete(
        f"/api/v1/payees/{payee['id']}/aliases/{destination['id']}"
        f"?version={alias_to_restore['version']}",
        headers=headers,
    ).json()
    restored_by_rename = client.patch(
        f"/api/v1/payees/{payee['id']}",
        headers=headers,
        json={"version": after_delete["version"], "name": "deleted destination"},
    )
    assert restored_by_rename.status_code == 200, restored_by_rename.text
    payee = restored_by_rename.json()
    assert sum(item["is_primary"] for item in payee["aliases"]) == 1
    promoted = next(item for item in payee["aliases"] if item["is_primary"])
    assert promoted["id"] == destination["id"]
    assert promoted["deleted_at"] is None

    other = _payee(client, headers, "Other Payee")
    reserved = client.post(
        f"/api/v1/payees/{other['id']}/aliases",
        headers=headers,
        json={"version": other["version"], "alias": "Reserved deleted alias"},
    ).json()
    reserved_alias = next(
        item for item in reserved["aliases"] if item["alias"] == "Reserved deleted alias"
    )
    reserved_deleted = client.delete(
        f"/api/v1/payees/{other['id']}/aliases/{reserved_alias['id']}"
        f"?version={reserved['version']}",
        headers=headers,
    )
    assert reserved_deleted.status_code == 200
    conflict = client.patch(
        f"/api/v1/payees/{payee['id']}",
        headers=headers,
        json={"version": payee["version"], "name": "RESERVED DELETED ALIAS"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "PAYEE_ALIAS_CONFLICT"
    after_conflict = client.get(f"/api/v1/payees/{payee['id']}", headers=headers).json()
    assert after_conflict["name"] == "deleted destination"
    assert sum(item["is_primary"] for item in after_conflict["aliases"]) == 1
    assert (
        next(item for item in after_conflict["aliases"] if item["is_primary"])["id"]
        == (destination["id"])
    )

    payee_audit = client.get(
        f"/api/v1/audit?entity_type=payee&entity_id={payee['id']}", headers=headers
    )
    alias_audit = client.get(
        f"/api/v1/audit?entity_type=payee_alias&entity_id={old_alias['id']}",
        headers=headers,
    )
    assert payee_audit.status_code == alias_audit.status_code == 200
    assert {item["action"] for item in payee_audit.json()["items"]} >= {
        "create",
        "update",
    }
    assert {item["action"] for item in alias_audit.json()["items"]} >= {
        "delete",
        "restore",
    }


def test_hash_collision_is_exactly_classified(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, headers = _register(client, "Hash collision")
    monkeypatch.setattr(payee_service, "normalized_alias_hash", lambda _value: "0" * 64)
    _payee(client, headers, "First hash value")

    same = client.post("/api/v1/payees", headers=headers, json={"name": "FIRST HASH VALUE"})
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "PAYEE_ALIAS_CONFLICT"

    collision = client.post(
        "/api/v1/payees", headers=headers, json={"name": "Different hash value"}
    )
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "PAYEE_ALIAS_HASH_COLLISION"


def test_transaction_payee_patch_hydration_filter_guards_and_google_contract(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Transaction Payee")
    account, category = _references(client, headers, "Payee transaction")
    payee = _payee(client, headers, "Canonical merchant")
    unlinked = _transaction(
        client,
        headers,
        account["id"],
        category["id"],
        payee_id=None,
        counterparty="Canonical merchant",
        occurred_at="2026-08-14T12:00:00Z",
    )
    assert unlinked["payee"] is None
    assert unlinked["counterparty"] == "Canonical merchant"
    transaction = _transaction(
        client,
        headers,
        account["id"],
        category["id"],
        payee_id=payee["id"],
    )
    assert transaction["payee"] == {"id": payee["id"], "name": "Canonical merchant"}
    assert transaction["counterparty"] == "Original statement text"

    omitted = client.patch(
        f"/api/v1/transactions/{transaction['id']}",
        headers=headers,
        json={"version": transaction["version"], "comment": "Payee omitted"},
    )
    assert omitted.status_code == 200, omitted.text
    transaction = omitted.json()
    assert transaction["payee"]["id"] == payee["id"]

    cleared = client.patch(
        f"/api/v1/transactions/{transaction['id']}",
        headers=headers,
        json={"version": transaction["version"], "payee_id": None},
    )
    assert cleared.status_code == 200, cleared.text
    transaction = cleared.json()
    assert transaction["payee"] is None

    assigned = client.patch(
        f"/api/v1/transactions/{transaction['id']}",
        headers=headers,
        json={"version": transaction["version"], "payee_id": payee["id"]},
    )
    assert assigned.status_code == 200, assigned.text
    transaction = assigned.json()
    filtered = client.get(f"/api/v1/transactions?payee_id={payee['id']}", headers=headers)
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [transaction["id"]]

    renamed = client.patch(
        f"/api/v1/payees/{payee['id']}",
        headers=headers,
        json={"version": payee["version"], "name": "Renamed merchant"},
    )
    assert renamed.status_code == 200
    historical = client.get(f"/api/v1/transactions/{transaction['id']}", headers=headers)
    assert historical.json()["payee"]["name"] == "Renamed merchant"
    assert historical.json()["counterparty"] == "Original statement text"

    deleted = client.delete(
        f"/api/v1/payees/{payee['id']}?version={renamed.json()['version']}",
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    deleted_payee = deleted.json()
    assert all(alias["deleted_at"] is None for alias in deleted_payee["aliases"])
    historical = client.get(f"/api/v1/transactions/{transaction['id']}", headers=headers)
    assert historical.status_code == 200
    assert historical.json()["payee"] == {"id": payee["id"], "name": "Renamed merchant"}

    rejected = client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": "2026-08-16T12:00:00Z",
            "transaction_type": "expense",
            "amount": "10.00",
            "currency": "RUB",
            "account_id": account["id"],
            "category_id": category["id"],
            "payee_id": payee["id"],
        },
    )
    assert rejected.status_code == 404
    assert rejected.json()["error"]["code"] == "PAYEE_NOT_FOUND"

    async def inspect_contract() -> tuple[bool, bool, dict | None]:
        async with AsyncSessionFactory() as session:
            row = await session.get(FinancialTransaction, uuid.UUID(transaction["id"]))
            assert row is not None
            payload_with_payee = transaction_payload(row)
            hash_with_payee = row_hash(payload_with_payee)
            saved_payee_id = row.payee_id
            row.payee_id = None
            payload_without_payee = transaction_payload(row)
            hash_without_payee = row_hash(payload_without_payee)
            row.payee_id = saved_payee_id
            await session.rollback()
            audit = await session.scalar(
                select(AuditLog)
                .where(
                    AuditLog.workspace_id == uuid.UUID(identity["workspace"]["id"]),
                    AuditLog.entity_type == "transaction",
                    AuditLog.entity_id == uuid.UUID(transaction["id"]),
                    AuditLog.action == "update",
                )
                .order_by(AuditLog.created_at.desc())
            )
            return (
                "payee_id" not in payload_with_payee,
                hash_with_payee == hash_without_payee,
                audit.after_data if audit else None,
            )

    no_google_field, same_hash, audit_after = asyncio.run(inspect_contract())
    assert no_google_field is True
    assert same_hash is True
    assert audit_after is not None and audit_after["payee_id"] == payee["id"]


def test_transaction_payee_respects_reconciliation_and_month_close(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Payee guards")
    account, category = _references(client, headers, "Payee guards")
    first = _payee(client, headers, "Guard merchant one")
    second = _payee(client, headers, "Guard merchant two")
    reconciled = _transaction(
        client,
        headers,
        account["id"],
        category["id"],
        payee_id=first["id"],
    )

    async def reconcile_directly() -> None:
        async with AsyncSessionFactory() as session:
            row = await session.get(FinancialTransaction, uuid.UUID(reconciled["id"]))
            assert row is not None
            row.status = "reconciled"
            await session.commit()

    asyncio.run(reconcile_directly())
    immutable = client.patch(
        f"/api/v1/transactions/{reconciled['id']}",
        headers=headers,
        json={"version": reconciled["version"], "payee_id": second["id"]},
    )
    assert immutable.status_code == 409
    assert immutable.json()["error"]["code"] == "RECONCILED_TRANSACTION_IMMUTABLE"

    closed = _transaction(
        client,
        headers,
        account["id"],
        category["id"],
        payee_id=first["id"],
        occurred_at="2026-07-15T12:00:00Z",
    )

    async def close_july_directly() -> None:
        async with AsyncSessionFactory() as session:
            control = await session.get(MonthCloseControl, uuid.UUID(identity["workspace"]["id"]))
            assert control is not None
            control.closed_through = date(2026, 7, 31)
            await session.commit()

    asyncio.run(close_july_directly())
    blocked = client.patch(
        f"/api/v1/transactions/{closed['id']}",
        headers=headers,
        json={"version": closed["version"], "payee_id": second["id"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "MONTH_CLOSED"


def test_recurring_explicit_payee_patch_execution_audit_and_archived_failure(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Recurring Payee")
    account, category = _references(client, headers, "Recurring Payee")
    payee = _payee(client, headers, "Recurring merchant")
    created = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json=_recurring_payload(account["id"], category["id"], payee_id=payee["id"]),
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["payee_id"] == payee["id"]
    assert rule["payee"] == {"id": payee["id"], "name": "Recurring merchant"}

    omitted = client.patch(
        f"/api/v1/recurring-rules/{rule['id']}",
        headers=headers,
        json={"version": rule["version"], "comment": "Updated comment"},
    )
    assert omitted.status_code == 200, omitted.text
    rule = omitted.json()
    assert rule["payee_id"] == payee["id"]

    cleared = client.patch(
        f"/api/v1/recurring-rules/{rule['id']}",
        headers=headers,
        json={"version": rule["version"], "payee_id": None},
    )
    assert cleared.status_code == 200, cleared.text
    rule = cleared.json()
    assert rule["payee_id"] is None and rule["payee"] is None

    reassigned = client.patch(
        f"/api/v1/recurring-rules/{rule['id']}",
        headers=headers,
        json={"version": rule["version"], "payee_id": payee["id"]},
    )
    assert reassigned.status_code == 200, reassigned.text
    rule = reassigned.json()
    archived_rule_response = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json=_recurring_payload(
            account["id"],
            category["id"],
            payee_id=payee["id"],
            name="Archived Payee failure",
        ),
    )
    assert archived_rule_response.status_code == 201, archived_rule_response.text
    archived_rule = archived_rule_response.json()
    executed = client.post(
        f"/api/v1/recurring-rules/{rule['id']}/run-now",
        headers={**headers, "X-Idempotency-Key": f"payee-run:{uuid.uuid4()}"},
    )
    assert executed.status_code == 200, executed.text
    transaction = client.get(
        f"/api/v1/transactions/{executed.json()['transaction_id']}", headers=headers
    )
    assert transaction.status_code == 200
    assert transaction.json()["payee"]["id"] == payee["id"]
    assert transaction.json()["counterparty"] == "Recurring statement snapshot"

    renamed = client.patch(
        f"/api/v1/payees/{payee['id']}",
        headers=headers,
        json={"version": payee["version"], "name": "Recurring merchant renamed"},
    )
    assert renamed.status_code == 200
    rule_read = client.get(f"/api/v1/recurring-rules/{rule['id']}", headers=headers)
    assert rule_read.json()["payee"]["name"] == "Recurring merchant renamed"
    assert rule_read.json()["counterparty"] == "Recurring statement snapshot"

    deleted = client.delete(
        f"/api/v1/payees/{payee['id']}?version={renamed.json()['version']}",
        headers=headers,
    )
    assert deleted.status_code == 200
    historical = client.get(f"/api/v1/recurring-rules/{rule['id']}", headers=headers)
    assert historical.status_code == 200
    assert historical.json()["payee"]["name"] == "Recurring merchant renamed"

    rejected_assignment = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json=_recurring_payload(
            account["id"],
            category["id"],
            payee_id=payee["id"],
            name="Archived Payee assignment rejected",
        ),
    )
    assert rejected_assignment.status_code == 404
    assert rejected_assignment.json()["error"]["code"] == "PAYEE_NOT_FOUND"

    before_count = client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]
    archived_before = client.get(
        f"/api/v1/recurring-rules/{archived_rule['id']}", headers=headers
    ).json()
    failed = client.post(
        f"/api/v1/recurring-rules/{archived_rule['id']}/run-now",
        headers={**headers, "X-Idempotency-Key": f"payee-archived:{uuid.uuid4()}"},
    )
    assert failed.status_code == 409, failed.text
    assert failed.json()["error"]["code"] == "RECURRING_RULE_INVALID"
    after_count = client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]
    assert after_count == before_count
    history = client.get(f"/api/v1/recurring-rules/{archived_rule['id']}/history", headers=headers)
    assert history.status_code == 200
    assert history.json()["items"][0]["status"] == "failed"
    archived_after = client.get(
        f"/api/v1/recurring-rules/{archived_rule['id']}", headers=headers
    ).json()
    assert archived_after["next_run_at"] == archived_before["next_run_at"]
    assert archived_after["last_run_at"] == archived_before["last_run_at"]

    audit = client.get(
        f"/api/v1/audit?entity_type=recurring_rule&entity_id={rule['id']}", headers=headers
    )
    assert audit.status_code == 200
    create_entry = next(
        item for item in audit.json()["items"] if item["action"] == "recurring.create"
    )
    assert create_entry["after_data"]["payee_id"] == payee["id"]
    assert create_entry["after_data"]["counterparty"] == "Recurring statement snapshot"
    assert create_entry["after_data"]["description"] == "Template description"
    assert create_entry["after_data"]["comment"] == "Template comment"


def test_workspace_composite_foreign_keys_reject_raw_cross_workspace_links(
    client: TestClient,
) -> None:
    identity_a, headers_a = _register(client, "Workspace A")
    _, headers_b = _register(client, "Workspace B")
    account_a, category_a = _references(client, headers_a, "Workspace A")
    payee_a = _payee(client, headers_a, "Workspace A Payee")
    payee_b = _payee(client, headers_b, "Workspace B Payee")
    transaction_a = _transaction(
        client,
        headers_a,
        account_a["id"],
        category_a["id"],
        payee_id=payee_a["id"],
    )
    recurring_a = client.post(
        "/api/v1/recurring-rules",
        headers=headers_a,
        json=_recurring_payload(
            account_a["id"], category_a["id"], payee_id=payee_a["id"], name="Raw FK rule"
        ),
    ).json()

    async def transaction_attack() -> bool:
        async with AsyncSessionFactory() as session:
            try:
                await session.execute(
                    update(FinancialTransaction)
                    .where(FinancialTransaction.id == uuid.UUID(transaction_a["id"]))
                    .values(payee_id=uuid.UUID(payee_b["id"]))
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return True
            return False

    async def recurring_attack() -> bool:
        async with AsyncSessionFactory() as session:
            try:
                await session.execute(
                    update(RecurringRule)
                    .where(RecurringRule.id == uuid.UUID(recurring_a["id"]))
                    .values(payee_id=uuid.UUID(payee_b["id"]))
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return True
            return False

    async def alias_attack() -> bool:
        async with AsyncSessionFactory() as session:
            session.add(
                PayeeAlias(
                    workspace_id=uuid.UUID(identity_a["workspace"]["id"]),
                    payee_id=uuid.UUID(payee_b["id"]),
                    alias="Cross workspace raw alias",
                    normalized_alias="cross workspace raw alias",
                    normalized_alias_hash="f" * 64,
                    is_primary=False,
                    created_by=uuid.UUID(identity_a["user"]["id"]),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return True
            return False

    assert asyncio.run(transaction_attack()) is True
    assert asyncio.run(recurring_attack()) is True
    assert asyncio.run(alias_attack()) is True

    cross_api = client.patch(
        f"/api/v1/transactions/{transaction_a['id']}",
        headers=headers_a,
        json={"version": transaction_a["version"], "payee_id": payee_b["id"]},
    )
    assert cross_api.status_code == 404
    assert cross_api.json()["error"]["code"] == "PAYEE_NOT_FOUND"
    cross_recurring_api = client.patch(
        f"/api/v1/recurring-rules/{recurring_a['id']}",
        headers=headers_a,
        json={"version": recurring_a["version"], "payee_id": payee_b["id"]},
    )
    assert cross_recurring_api.status_code == 404
    assert cross_recurring_api.json()["error"]["code"] == "PAYEE_NOT_FOUND"


def test_alias_concurrency_and_rename_race_leave_one_namespace_winner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Alias race")
    first = _payee(client, headers, "Race first")
    second = _payee(client, headers, "Race second")

    async def context(session_user_id: uuid.UUID, workspace_id: uuid.UUID) -> RequestContext:
        async with AsyncSessionFactory() as session:
            user = await session.get(User, session_user_id)
            workspace = await session.get(Workspace, workspace_id)
            assert user is not None and workspace is not None
            return RequestContext(
                user=user,
                workspace=workspace,
                role="owner",
                request_id=str(uuid.uuid4()),
            )

    user_id = uuid.UUID(identity["user"]["id"])
    workspace_id = uuid.UUID(identity["workspace"]["id"])
    request_context = asyncio.run(context(user_id, workspace_id))
    original_find = payee_service.repository.find_alias_candidate
    barrier_hash = alias_identity("Concurrent shared alias")[1]
    barrier_count = 0
    barrier_event: asyncio.Event | None = None

    async def synchronized_find(
        session: AsyncSession,
        candidate_workspace_id: uuid.UUID,
        candidate_hash: str,
    ) -> PayeeAlias | None:
        nonlocal barrier_count, barrier_event
        result = await original_find(session, candidate_workspace_id, candidate_hash)
        if candidate_hash == barrier_hash and result is None and barrier_count < 2:
            if barrier_event is None:
                barrier_event = asyncio.Event()
            barrier_count += 1
            if barrier_count == 2:
                barrier_event.set()
            await barrier_event.wait()
        return result

    monkeypatch.setattr(payee_service.repository, "find_alias_candidate", synchronized_find)

    async def add_alias(payee: dict, alias: str) -> str:
        async with AsyncSessionFactory() as session:
            try:
                await payee_service.create_alias(
                    session,
                    request_context,
                    uuid.UUID(payee["id"]),
                    PayeeAliasCreate(version=payee["version"], alias=alias),
                )
                return "winner"
            except ApiError as exc:
                return exc.code

    async def run_alias_race() -> list[str]:
        return list(
            await asyncio.gather(
                add_alias(first, "Concurrent shared alias"),
                add_alias(second, "CONCURRENT\u2003SHARED ALIAS"),
            )
        )

    results = asyncio.run(run_alias_race())
    assert sorted(results) == ["PAYEE_ALIAS_CONFLICT", "winner"]

    collision_first = _payee(client, headers, "Collision race first")
    collision_second = _payee(client, headers, "Collision race second")
    original_hash = payee_service.normalized_alias_hash
    barrier_hash = "c" * 64
    barrier_count = 0
    barrier_event = None
    monkeypatch.setattr(
        payee_service,
        "normalized_alias_hash",
        lambda _value: barrier_hash,
    )

    async def run_collision_race() -> list[str]:
        return list(
            await asyncio.gather(
                add_alias(collision_first, "Collision hash value A"),
                add_alias(collision_second, "Collision hash value B"),
            )
        )

    collision_results = asyncio.run(run_collision_race())
    assert sorted(collision_results) == ["PAYEE_ALIAS_HASH_COLLISION", "winner"]
    monkeypatch.setattr(payee_service, "normalized_alias_hash", original_hash)

    third = _payee(client, headers, "Rename race first")
    fourth = _payee(client, headers, "Rename race second")
    barrier_hash = alias_identity("Rename versus add")[1]
    barrier_count = 0
    barrier_event = None

    async def rename(payee: dict) -> str:
        async with AsyncSessionFactory() as session:
            try:
                await payee_service.update_payee(
                    session,
                    request_context,
                    uuid.UUID(payee["id"]),
                    PayeeUpdate(version=payee["version"], name="Rename versus add"),
                )
                return "winner"
            except ApiError as exc:
                return exc.code

    async def add_for_fourth() -> str:
        return await add_alias(fourth, "RENAME VERSUS ADD")

    async def run_rename_race() -> list[str]:
        return list(await asyncio.gather(rename(third), add_for_fourth()))

    rename_results = asyncio.run(run_rename_race())
    assert sorted(rename_results) == ["PAYEE_ALIAS_CONFLICT", "winner"]

    async def inspect_namespace() -> tuple[int, dict[uuid.UUID, int]]:
        normalized, digest = alias_identity("Rename versus add")
        async with AsyncSessionFactory() as session:
            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(PayeeAlias)
                    .where(
                        PayeeAlias.workspace_id == workspace_id,
                        PayeeAlias.normalized_alias_hash == digest,
                        PayeeAlias.normalized_alias == normalized,
                    )
                )
                or 0
            )
            rows = list(
                (
                    await session.execute(
                        select(PayeeAlias.payee_id, func.count())
                        .where(
                            PayeeAlias.workspace_id == workspace_id,
                            PayeeAlias.is_primary.is_(True),
                        )
                        .group_by(PayeeAlias.payee_id)
                    )
                ).all()
            )
            return count, {payee_id: int(primary_count) for payee_id, primary_count in rows}

    namespace_count, primary_counts = asyncio.run(inspect_namespace())
    assert namespace_count == 1
    assert all(count == 1 for count in primary_counts.values())


def test_payee_list_and_transaction_page_use_bounded_payee_queries(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Payee queries")
    account, category = _references(client, headers, "Payee queries")
    payees = [_payee(client, headers, f"Query Payee {index}") for index in range(5)]
    for index, payee in enumerate(payees):
        alias_response = client.post(
            f"/api/v1/payees/{payee['id']}/aliases",
            headers=headers,
            json={"version": payee["version"], "alias": f"Query Alias {index}"},
        )
        assert alias_response.status_code == 201
        _transaction(
            client,
            headers,
            account["id"],
            category["id"],
            payee_id=payee["id"],
            counterparty=f"Statement {index}",
        )

    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement.lower())

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        listed = client.get("/api/v1/payees?limit=100", headers=headers)
        transactions = client.get("/api/v1/transactions?limit=100", headers=headers)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)
    assert listed.status_code == transactions.status_code == 200
    assert len(listed.json()["items"]) == 5
    payee_list_queries = [
        statement
        for statement in statements
        if "from payees" in statement and "transactions" not in statement
    ]
    transaction_payee_queries = [
        statement
        for statement in statements
        if "from payees" in statement and "payees.id in" in statement
    ]
    assert len(payee_list_queries) <= 3
    assert len(transaction_payee_queries) == 1


def test_payee_schema_constraints_and_no_rules_leak() -> None:
    async def inspect_schema() -> dict[str, object]:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "transaction_columns": {
                        item["name"]
                        for item in inspect(sync_connection).get_columns("transactions")
                    },
                    "recurring_columns": {
                        item["name"]
                        for item in inspect(sync_connection).get_columns("recurring_rules")
                    },
                    "transaction_fks": {
                        item["name"]: (item["constrained_columns"], item["referred_columns"])
                        for item in inspect(sync_connection).get_foreign_keys("transactions")
                    },
                    "recurring_fks": {
                        item["name"]: (item["constrained_columns"], item["referred_columns"])
                        for item in inspect(sync_connection).get_foreign_keys("recurring_rules")
                    },
                    "alias_fks": {
                        item["name"]: (item["constrained_columns"], item["referred_columns"])
                        for item in inspect(sync_connection).get_foreign_keys("payee_aliases")
                    },
                    "alias_indexes": {
                        item["name"]: item
                        for item in inspect(sync_connection).get_indexes("payee_aliases")
                    },
                }
            )

    schema = asyncio.run(inspect_schema())
    tables = schema["tables"]
    assert isinstance(tables, set)
    assert {"payees", "payee_aliases"} <= tables
    assert "categorization_rules" not in tables
    assert "categorization_rule_conditions" not in tables
    assert "payee_id" in schema["transaction_columns"]
    assert "payee_id" in schema["recurring_columns"]
    assert schema["transaction_fks"]["fk_transactions_payee_workspace"] == (
        ["payee_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    assert schema["recurring_fks"]["fk_recurring_rules_payee_workspace"] == (
        ["payee_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    assert schema["alias_fks"]["fk_payee_aliases_payee_workspace"] == (
        ["payee_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    primary_index = schema["alias_indexes"]["uq_payee_aliases_active_primary"]
    assert primary_index["unique"] is True
    assert primary_index["column_names"] == ["payee_id"]


def test_viewer_can_read_but_cannot_mutate_payees(client: TestClient) -> None:
    identity, headers = _register(client, "Viewer Payee")
    payee = _payee(client, headers, "Viewer visible")

    async def viewer_headers() -> dict[str, str]:
        async with AsyncSessionFactory() as session:
            email = f"viewer-{uuid.uuid4()}@example.com"
            viewer = User(
                email=email,
                normalized_email=email,
                display_name="Payee Viewer",
            )
            session.add(viewer)
            await session.flush()
            await session.execute(
                text(
                    "INSERT INTO workspace_members (workspace_id, user_id, role) "
                    "VALUES (:workspace_id, :user_id, 'viewer')"
                ),
                {
                    "workspace_id": identity["workspace"]["id"],
                    "user_id": str(viewer.id),
                },
            )
            await session.commit()
            return {
                "X-User-ID": str(viewer.id),
                "X-Workspace-ID": identity["workspace"]["id"],
            }

    viewer = asyncio.run(viewer_headers())
    assert client.get("/api/v1/payees", headers=viewer).status_code == 200
    assert client.get(f"/api/v1/payees/{payee['id']}", headers=viewer).status_code == 200
    denied = client.patch(
        f"/api/v1/payees/{payee['id']}",
        headers=viewer,
        json={"version": payee["version"], "notes": "Denied"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "INSUFFICIENT_ROLE"
