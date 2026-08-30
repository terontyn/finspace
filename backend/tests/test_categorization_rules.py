import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.services.categorization_rules import normalize_match_text

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _configure_categorization_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _register(client: TestClient, label: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"categorization-{uuid.uuid4()}@example.com",
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


def _payee(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post(
        "/api/v1/payees",
        headers=headers,
        json={"name": name},
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
        json={
            "name": name,
            "priority": priority,
            "category_id": category_id,
            **matchers,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_uncategorized(
    identity: dict,
    account_id: str,
    *,
    counterparty: str | None = None,
    description: str | None = None,
    payee_id: str | None = None,
    transaction_type: str = "expense",
) -> str:
    async with AsyncSessionFactory() as session:
        transaction = FinancialTransaction(
            workspace_id=uuid.UUID(identity["workspace"]["id"]),
            occurred_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            transaction_type=transaction_type,
            amount=Decimal("1250.2500"),
            currency="RUB",
            account_id=uuid.UUID(account_id),
            category_id=None,
            payee_id=uuid.UUID(payee_id) if payee_id else None,
            counterparty=counterparty,
            description=description,
            status="confirmed",
            source="import",
            created_by=uuid.UUID(identity["user"]["id"]),
            updated_by=uuid.UUID(identity["user"]["id"]),
        )
        session.add(transaction)
        await session.commit()
        await session.refresh(transaction)
        return str(transaction.id)


async def _set_role(identity: dict, role: str) -> None:
    async with AsyncSessionFactory() as session:
        member = await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == uuid.UUID(identity["workspace"]["id"]),
                WorkspaceMember.user_id == uuid.UUID(identity["user"]["id"]),
            )
        )
        assert member is not None
        member.role = role
        await session.commit()


async def _categorization_audit(transaction_id: str) -> AuditLog | None:
    async with AsyncSessionFactory() as session:
        return await session.scalar(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "transaction",
                AuditLog.entity_id == uuid.UUID(transaction_id),
                AuditLog.source == "categorization_rule",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )


def test_match_normalization_is_unicode_conservative() -> None:
    assert normalize_match_text("  IKEA\u2003Екатеринбург ") == normalize_match_text(  # noqa: RUF001
        "ikea Екатеринбург"
    )
    assert normalize_match_text("Ａｍａｚｏｎ 1234") == "amazon 1234"  # noqa: RUF001
    assert normalize_match_text("A-B") != normalize_match_text("AB")


def test_rule_crud_versioning_and_workspace_isolation(client: TestClient) -> None:
    _, headers = _register(client, "Rules owner")
    _, other_headers = _register(client, "Rules other")
    category = _category(client, headers, "Groceries")

    created = _rule(
        client,
        headers,
        name="Groceries rule",
        category_id=category["id"],
        transaction_type="expense",
        counterparty_contains="market",
    )
    assert created["version"] == 1

    listed = client.get("/api/v1/categorization-rules", headers=headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [created["id"]]

    hidden = client.get(f"/api/v1/categorization-rules/{created['id']}", headers=other_headers)
    assert hidden.status_code == 404

    updated = client.patch(
        f"/api/v1/categorization-rules/{created['id']}",
        headers=headers,
        json={"version": created["version"], "priority": 20},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["priority"] == 20
    assert updated.json()["version"] == 2

    stale = client.patch(
        f"/api/v1/categorization-rules/{created['id']}",
        headers=headers,
        json={"version": 1, "priority": 30},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    deleted = client.delete(
        f"/api/v1/categorization-rules/{created['id']}?version=2",
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    deleted_rule = deleted.json()
    assert deleted_rule["deleted_at"] is not None
    assert deleted_rule["version"] == 3

    active_list = client.get("/api/v1/categorization-rules", headers=headers)
    assert active_list.json()["items"] == []
    archived_list = client.get(
        "/api/v1/categorization-rules?include_deleted=true",
        headers=headers,
    )
    assert archived_list.json()["page"]["total"] == 1

    restored = client.post(
        f"/api/v1/categorization-rules/{created['id']}/restore",
        headers=headers,
        json={"version": 3},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None
    assert restored.json()["version"] == 4


def test_rule_validation_rejects_missing_matcher_and_wrong_category_type(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Rules validation")
    expense = _category(client, headers, "Expense", "expense")
    income = _category(client, headers, "Income", "income")

    missing = client.post(
        "/api/v1/categorization-rules",
        headers=headers,
        json={"name": "No matcher", "category_id": expense["id"]},
    )
    assert missing.status_code == 422

    mismatch = client.post(
        "/api/v1/categorization-rules",
        headers=headers,
        json={
            "name": "Wrong category",
            "transaction_type": "expense",
            "category_id": income["id"],
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "INVALID_CATEGORY_TYPE"

    transfer = client.post(
        "/api/v1/categorization-rules",
        headers=headers,
        json={
            "name": "Transfer is not categorization",
            "transaction_type": "transfer",
            "category_id": expense["id"],
        },
    )
    assert transfer.status_code == 422


def test_preview_uses_and_semantics_priority_and_inactive_skip(client: TestClient) -> None:
    identity, headers = _register(client, "Rules matching")
    account = _account(client, headers, "Main card")
    first_category = _category(client, headers, "First")
    winner_category = _category(client, headers, "Winner")

    _rule(
        client,
        headers,
        name="Fails AND condition",
        category_id=first_category["id"],
        priority=1,
        transaction_type="expense",
        counterparty_contains="ikea",
        description_contains="impossible",
    )
    disabled = _rule(
        client,
        headers,
        name="Disabled higher priority",
        category_id=first_category["id"],
        priority=2,
        counterparty_contains="ikea",
    )
    disabled_response = client.patch(
        f"/api/v1/categorization-rules/{disabled['id']}",
        headers=headers,
        json={"version": disabled["version"], "is_active": False},
    )
    assert disabled_response.status_code == 200
    winner = _rule(
        client,
        headers,
        name="Unicode winner",
        category_id=winner_category["id"],
        priority=10,
        account_id=account["id"],
        counterparty_contains="amazon 1234",
    )
    _rule(
        client,
        headers,
        name="Lower priority fallback",
        category_id=first_category["id"],
        priority=20,
        counterparty_contains="amazon",
    )
    transaction_id = asyncio.run(
        _insert_uncategorized(
            identity,
            account["id"],
            counterparty="  Ａｍａｚｏｎ   1234 store  ",  # noqa: RUF001
            description="Furniture",
        )
    )

    preview = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["matched"] is True
    assert payload["rule"]["id"] == winner["id"]
    assert payload["category"]["id"] == winner_category["id"]


def test_payee_condition_never_infers_from_counterparty(client: TestClient) -> None:
    identity, headers = _register(client, "Rules Payee")
    account = _account(client, headers, "Payee card")
    category = _category(client, headers, "Payee target")
    payee = _payee(client, headers, "Exact Shop")
    _rule(
        client,
        headers,
        name="Explicit Payee only",
        category_id=category["id"],
        payee_id=payee["id"],
    )

    raw_only_id = asyncio.run(
        _insert_uncategorized(
            identity,
            account["id"],
            counterparty="Exact Shop",
            payee_id=None,
        )
    )
    raw_preview = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": raw_only_id},
    )
    assert raw_preview.status_code == 200
    assert raw_preview.json()["matched"] is False

    explicit_id = asyncio.run(
        _insert_uncategorized(
            identity,
            account["id"],
            counterparty="Completely different raw text",
            payee_id=payee["id"],
        )
    )
    explicit_preview = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": explicit_id},
    )
    assert explicit_preview.status_code == 200
    assert explicit_preview.json()["matched"] is True


def test_apply_is_explicit_versioned_and_does_not_overwrite_category(client: TestClient) -> None:
    identity, headers = _register(client, "Rules apply")
    account = _account(client, headers, "Apply card")
    original_category = _category(client, headers, "Original category")
    target_category = _category(client, headers, "Target category")
    _rule(
        client,
        headers,
        name="Apply shop",
        category_id=target_category["id"],
        priority=1,
        transaction_type="expense",
        counterparty_contains="shop",
    )

    transaction_id = asyncio.run(
        _insert_uncategorized(
            identity,
            account["id"],
            counterparty="SHOP statement raw text",
        )
    )
    applied = client.post(
        f"/api/v1/transactions/{transaction_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert applied.status_code == 200, applied.text
    applied_payload = applied.json()
    assert applied_payload["applied"] is True
    assert applied_payload["reason"] == "applied"
    assert applied_payload["transaction"]["category"]["id"] == target_category["id"]
    assert applied_payload["transaction"]["counterparty"] == "SHOP statement raw text"
    assert applied_payload["transaction"]["version"] == 2
    audit = asyncio.run(_categorization_audit(transaction_id))
    assert audit is not None

    second = client.post(
        f"/api/v1/transactions/{transaction_id}/apply-categorization",
        headers=headers,
        json={"version": 2},
    )
    assert second.status_code == 200
    assert second.json()["applied"] is False
    assert second.json()["reason"] == "already_categorized"
    assert second.json()["transaction"]["version"] == 2

    existing = client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": "2026-08-16T12:00:00Z",
            "transaction_type": "expense",
            "amount": "500.00",
            "currency": "RUB",
            "account_id": account["id"],
            "category_id": original_category["id"],
            "counterparty": "Shop already categorized",
            "status": "confirmed",
            "source": "manual",
        },
    )
    assert existing.status_code == 201, existing.text
    existing_payload = existing.json()
    protected = client.post(
        f"/api/v1/transactions/{existing_payload['id']}/apply-categorization",
        headers=headers,
        json={"version": existing_payload["version"]},
    )
    assert protected.status_code == 200
    assert protected.json()["reason"] == "already_categorized"
    assert protected.json()["transaction"]["category"]["id"] == original_category["id"]

    stale = client.post(
        f"/api/v1/transactions/{transaction_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_viewer_can_preview_but_cannot_mutate_rules_or_transactions(client: TestClient) -> None:
    identity, headers = _register(client, "Rules viewer")
    account = _account(client, headers, "Viewer card")
    category = _category(client, headers, "Viewer category")
    _rule(
        client,
        headers,
        name="Viewer visible rule",
        category_id=category["id"],
        counterparty_contains="viewer",
    )
    transaction_id = asyncio.run(
        _insert_uncategorized(identity, account["id"], counterparty="viewer transaction")
    )
    asyncio.run(_set_role(identity, "viewer"))

    listing = client.get("/api/v1/categorization-rules", headers=headers)
    assert listing.status_code == 200
    preview = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert preview.status_code == 200
    assert preview.json()["matched"] is True

    create = client.post(
        "/api/v1/categorization-rules",
        headers=headers,
        json={
            "name": "Viewer cannot create",
            "category_id": category["id"],
            "counterparty_contains": "x",
        },
    )
    assert create.status_code == 403
    assert create.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    apply = client.post(
        f"/api/v1/transactions/{transaction_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert apply.status_code == 403
    assert apply.json()["error"]["code"] == "INSUFFICIENT_ROLE"
