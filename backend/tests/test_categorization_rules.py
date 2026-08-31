import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.audit import AuditLog
from app.db.models.categories import Category
from app.db.models.categorization_rule_sets import CategorizationRuleSetControl
from app.db.models.categorization_rules import CategorizationRule
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.dependencies.context import RequestContext
from app.repositories import categorization_rules as categorization_repository
from app.schemas.categorization_rules import CategorizationRuleCreate, CategorizationRuleUpdate
from app.services import categorization_rules as categorization_service
from app.services.categorization_rules import normalize_match_text
from app.services.financial_period_guard import get_or_create_control

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


async def _set_transaction_status(transaction_id: str, status: str) -> None:
    async with AsyncSessionFactory() as session:
        transaction = await session.get(FinancialTransaction, uuid.UUID(transaction_id))
        assert transaction is not None
        transaction.status = status
        await session.commit()


async def _close_workspace_through(identity: dict, closed_through: date) -> None:
    async with AsyncSessionFactory() as session:
        control = await get_or_create_control(
            session,
            uuid.UUID(identity["workspace"]["id"]),
            for_update=True,
        )
        control.closed_through = closed_through
        await session.commit()


async def _categorization_audit(transaction_id: str) -> AuditLog | None:
    async with AsyncSessionFactory() as session:
        return await session.scalar(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "transaction",
                AuditLog.entity_id == uuid.UUID(transaction_id),
                AuditLog.action == "update",
                AuditLog.source == "api",
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
    other_category = _category(client, other_headers, "Other workspace category")

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

    cross_workspace = client.post(
        "/api/v1/categorization-rules",
        headers=headers,
        json={
            "name": "Foreign category",
            "category_id": other_category["id"],
            "counterparty_contains": "foreign",
        },
    )
    assert cross_workspace.status_code == 404
    assert cross_workspace.json()["error"]["code"] == "CATEGORY_NOT_FOUND"

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


def test_preview_uses_and_semantics_priority_and_inactive_deleted_skip(
    client: TestClient,
) -> None:
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
        counterparty_contains="amazon",
    )
    disabled_response = client.patch(
        f"/api/v1/categorization-rules/{disabled['id']}",
        headers=headers,
        json={"version": disabled["version"], "is_active": False},
    )
    assert disabled_response.status_code == 200

    deleted = _rule(
        client,
        headers,
        name="Deleted higher priority",
        category_id=first_category["id"],
        priority=3,
        counterparty_contains="amazon",
    )
    deleted_response = client.delete(
        f"/api/v1/categorization-rules/{deleted['id']}?version={deleted['version']}",
        headers=headers,
    )
    assert deleted_response.status_code == 200

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

    unchanged = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
    assert unchanged.status_code == 200
    assert unchanged.json()["category"] is None
    assert unchanged.json()["version"] == 1


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


def test_apply_is_explicit_versioned_and_does_not_overwrite_category_or_splits(
    client: TestClient,
) -> None:
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
    assert audit.after_data is not None
    assert audit.after_data["category_id"] == target_category["id"]

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

    split = client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": "2026-08-17T12:00:00Z",
            "transaction_type": "expense",
            "amount": "500.00",
            "currency": "RUB",
            "account_id": account["id"],
            "counterparty": "Shop split categorized",
            "status": "confirmed",
            "source": "manual",
            "splits": [
                {
                    "category_id": original_category["id"],
                    "amount": "500.00",
                }
            ],
        },
    )
    assert split.status_code == 201, split.text
    split_payload = split.json()
    split_protected = client.post(
        f"/api/v1/transactions/{split_payload['id']}/apply-categorization",
        headers=headers,
        json={"version": split_payload["version"]},
    )
    assert split_protected.status_code == 200, split_protected.text
    assert split_protected.json()["reason"] == "already_categorized"
    assert split_protected.json()["transaction"]["version"] == split_payload["version"]
    assert len(split_protected.json()["transaction"]["splits"]) == 1

    no_match_id = asyncio.run(
        _insert_uncategorized(
            identity,
            account["id"],
            counterparty="Completely unrelated statement",
        )
    )
    no_match = client.post(
        f"/api/v1/transactions/{no_match_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert no_match.status_code == 200, no_match.text
    assert no_match.json()["applied"] is False
    assert no_match.json()["reason"] == "no_match"
    assert no_match.json()["transaction"]["category"] is None
    assert no_match.json()["transaction"]["version"] == 1

    stale = client.post(
        f"/api/v1/transactions/{transaction_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_apply_preserves_reconciled_and_closed_period_guards(client: TestClient) -> None:
    identity, headers = _register(client, "Rules guards")
    account = _account(client, headers, "Guard card")
    category = _category(client, headers, "Guard category")
    _rule(
        client,
        headers,
        name="Guard rule",
        category_id=category["id"],
        counterparty_contains="guard",
    )

    reconciled_id = asyncio.run(
        _insert_uncategorized(
            identity,
            account["id"],
            counterparty="guard reconciled",
        )
    )
    asyncio.run(_set_transaction_status(reconciled_id, "reconciled"))
    reconciled = client.post(
        f"/api/v1/transactions/{reconciled_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert reconciled.status_code == 409
    assert reconciled.json()["error"]["code"] == "RECONCILED_TRANSACTION_IMMUTABLE"

    closed_id = asyncio.run(
        _insert_uncategorized(
            identity,
            account["id"],
            counterparty="guard closed period",
        )
    )
    asyncio.run(_close_workspace_through(identity, date(2026, 8, 31)))
    closed = client.post(
        f"/api/v1/transactions/{closed_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert closed.status_code == 409
    assert closed.json()["error"]["code"] == "MONTH_CLOSED"


def test_apply_rejects_a_rule_changed_after_matching(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Rules concurrent mutation")
    account = _account(client, headers, "Concurrent card")
    original_category = _category(client, headers, "Concurrent original")
    replacement_category = _category(client, headers, "Concurrent replacement")
    rule = _rule(
        client,
        headers,
        name="Concurrent rule",
        category_id=original_category["id"],
        counterparty_contains="concurrent shop",
    )
    transaction_id = asyncio.run(
        _insert_uncategorized(
            identity,
            account["id"],
            counterparty="Concurrent Shop statement",
        )
    )

    preview_finished = asyncio.Event()
    allow_apply_to_continue = asyncio.Event()
    original_preview = categorization_service.preview_transaction

    async def paused_preview(
        session: AsyncSession,
        workspace_id: uuid.UUID,
        selected_transaction_id: uuid.UUID,
    ) -> tuple[FinancialTransaction, categorization_service.CategorizationMatch | None]:
        result = await original_preview(session, workspace_id, selected_transaction_id)
        preview_finished.set()
        await allow_apply_to_continue.wait()
        return result

    monkeypatch.setattr(categorization_service, "preview_transaction", paused_preview)

    async def run_race() -> str:
        async with AsyncSessionFactory() as apply_session:
            apply_user = await apply_session.get(User, uuid.UUID(identity["user"]["id"]))
            apply_workspace = await apply_session.get(
                Workspace,
                uuid.UUID(identity["workspace"]["id"]),
            )
            assert apply_user is not None
            assert apply_workspace is not None
            apply_context = RequestContext(
                user=apply_user,
                workspace=apply_workspace,
                role="owner",
                request_id=str(uuid.uuid4()),
            )
            apply_task = asyncio.create_task(
                categorization_service.apply_to_transaction(
                    apply_session,
                    apply_context,
                    uuid.UUID(transaction_id),
                    1,
                )
            )
            await preview_finished.wait()

            async with AsyncSessionFactory() as update_session:
                update_user = await update_session.get(User, apply_user.id)
                update_workspace = await update_session.get(Workspace, apply_workspace.id)
                assert update_user is not None
                assert update_workspace is not None
                update_context = RequestContext(
                    user=update_user,
                    workspace=update_workspace,
                    role="owner",
                    request_id=str(uuid.uuid4()),
                )
                await categorization_service.update_rule(
                    update_session,
                    update_context,
                    uuid.UUID(rule["id"]),
                    CategorizationRuleUpdate(
                        version=rule["version"],
                        category_id=uuid.UUID(replacement_category["id"]),
                    ),
                )

            allow_apply_to_continue.set()
            try:
                await apply_task
            except ApiError as exc:
                await apply_session.rollback()
                return exc.code
            raise AssertionError("Concurrent rule mutation was not rejected")

    assert asyncio.run(run_race()) == "CATEGORIZATION_RULE_CHANGED"

    unchanged = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["category"] is None
    assert unchanged.json()["version"] == 1
    assert asyncio.run(_categorization_audit(transaction_id)) is None

    current_rule = client.get(
        f"/api/v1/categorization-rules/{rule['id']}",
        headers=headers,
    )
    assert current_rule.status_code == 200, current_rule.text
    assert current_rule.json()["category_id"] == replacement_category["id"]
    assert current_rule.json()["version"] == 2


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


async def _set_rule_created_at(rule_id: str, moment: datetime) -> None:
    async with AsyncSessionFactory() as session:
        await session.execute(
            update(CategorizationRule)
            .where(CategorizationRule.id == uuid.UUID(rule_id))
            .values(created_at=moment)
        )
        await session.commit()


async def _archive_category(category_id: str) -> None:
    async with AsyncSessionFactory() as session:
        await session.execute(
            update(Category).where(Category.id == uuid.UUID(category_id)).values(is_archived=True)
        )
        await session.commit()


async def _insert_transfer(
    identity: dict,
    account_id: str,
    target_account_id: str,
    *,
    counterparty: str,
) -> str:
    async with AsyncSessionFactory() as session:
        transaction = FinancialTransaction(
            workspace_id=uuid.UUID(identity["workspace"]["id"]),
            occurred_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            transaction_type="transfer",
            amount=Decimal("500.0000"),
            currency="RUB",
            account_id=uuid.UUID(account_id),
            target_account_id=uuid.UUID(target_account_id),
            category_id=None,
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


def test_equal_priority_matches_break_ties_by_created_at_then_id(client: TestClient) -> None:
    identity, headers = _register(client, "Rules ties")
    account = _account(client, headers, "Tie card")
    earlier_category = _category(client, headers, "Earlier")
    later_category = _category(client, headers, "Later")

    earlier = _rule(
        client,
        headers,
        name="Tie earlier",
        category_id=earlier_category["id"],
        priority=7,
        counterparty_contains="tie shop",
    )
    later = _rule(
        client,
        headers,
        name="Tie later",
        category_id=later_category["id"],
        priority=7,
        counterparty_contains="tie shop",
    )
    transaction_id = asyncio.run(
        _insert_uncategorized(identity, account["id"], counterparty="Tie Shop 77")
    )

    base = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    asyncio.run(_set_rule_created_at(earlier["id"], base))
    asyncio.run(_set_rule_created_at(later["id"], base.replace(hour=10)))

    first = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert first.status_code == 200, first.text
    assert first.json()["rule"]["id"] == earlier["id"]

    # Identical priority and identical created_at must still resolve deterministically by id.
    asyncio.run(_set_rule_created_at(later["id"], base))
    expected_id = min(earlier["id"], later["id"], key=lambda value: uuid.UUID(value).bytes)

    second = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert second.status_code == 200, second.text
    assert second.json()["rule"]["id"] == expected_id

    repeated = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert repeated.json()["rule"]["id"] == expected_id


def test_transfer_transactions_never_match_categorization_rules(client: TestClient) -> None:
    identity, headers = _register(client, "Rules transfer")
    source_account = _account(client, headers, "Transfer source")
    target_account = _account(client, headers, "Transfer target")
    category = _category(client, headers, "Transfer target category")

    _rule(
        client,
        headers,
        name="Broad transfer matcher",
        category_id=category["id"],
        priority=1,
        counterparty_contains="internal move",
    )
    transaction_id = asyncio.run(
        _insert_transfer(
            identity,
            source_account["id"],
            target_account["id"],
            counterparty="Internal Move 42",
        )
    )

    preview = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["matched"] is False
    assert preview.json()["rule"] is None

    applied = client.post(
        f"/api/v1/transactions/{transaction_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert applied.status_code == 200, applied.text
    payload = applied.json()
    assert payload["applied"] is False
    assert payload["reason"] == "no_match"
    assert payload["transaction"]["category"] is None
    assert payload["transaction"]["version"] == 1


def test_rule_pointing_at_archived_category_is_skipped(client: TestClient) -> None:
    identity, headers = _register(client, "Rules archived")
    account = _account(client, headers, "Archived card")
    archived_category = _category(client, headers, "Archived target")
    active_category = _category(client, headers, "Active target")

    archived_rule = _rule(
        client,
        headers,
        name="Higher priority archived target",
        category_id=archived_category["id"],
        priority=1,
        counterparty_contains="archive shop",
    )
    active_rule = _rule(
        client,
        headers,
        name="Lower priority active target",
        category_id=active_category["id"],
        priority=2,
        counterparty_contains="archive shop",
    )
    transaction_id = asyncio.run(
        _insert_uncategorized(identity, account["id"], counterparty="Archive Shop 5")
    )

    before = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert before.json()["rule"]["id"] == archived_rule["id"]

    asyncio.run(_archive_category(archived_category["id"]))

    after = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert after.status_code == 200, after.text
    assert after.json()["matched"] is True
    assert after.json()["rule"]["id"] == active_rule["id"]
    assert after.json()["category"]["id"] == active_category["id"]

    applied = client.post(
        f"/api/v1/transactions/{transaction_id}/apply-categorization",
        headers=headers,
        json={"version": 1},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert applied.json()["transaction"]["category"]["id"] == active_category["id"]


def test_categorization_rule_payee_reference_is_workspace_scoped(client: TestClient) -> None:
    identity_a, headers_a = _register(client, "Rules workspace A")
    _, headers_b = _register(client, "Rules workspace B")
    account_a = _account(client, headers_a, "Workspace A card")
    category_a = _category(client, headers_a, "Workspace A category")
    payee_a = _payee(client, headers_a, "Workspace A payee")
    payee_b = _payee(client, headers_b, "Workspace B payee")

    rule_a = _rule(
        client,
        headers_a,
        name="Workspace A payee rule",
        category_id=category_a["id"],
        priority=1,
        payee_id=payee_a["id"],
    )

    async def update_attack() -> bool:
        async with AsyncSessionFactory() as session:
            try:
                await session.execute(
                    update(CategorizationRule)
                    .where(CategorizationRule.id == uuid.UUID(rule_a["id"]))
                    .values(payee_id=uuid.UUID(payee_b["id"]))
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return True
            return False

    async def insert_attack() -> bool:
        async with AsyncSessionFactory() as session:
            session.add(
                CategorizationRule(
                    workspace_id=uuid.UUID(identity_a["workspace"]["id"]),
                    name="Raw cross workspace rule",
                    priority=1,
                    is_active=True,
                    payee_id=uuid.UUID(payee_b["id"]),
                    category_id=uuid.UUID(category_a["id"]),
                    created_by=uuid.UUID(identity_a["user"]["id"]),
                    updated_by=uuid.UUID(identity_a["user"]["id"]),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return True
            return False

    assert asyncio.run(update_attack()) is True
    assert asyncio.run(insert_attack()) is True

    cross_create = client.post(
        "/api/v1/categorization-rules",
        headers=headers_a,
        json={
            "name": "Cross workspace payee rule",
            "priority": 1,
            "category_id": category_a["id"],
            "payee_id": payee_b["id"],
        },
    )
    assert cross_create.status_code == 404, cross_create.text

    cross_account = client.post(
        "/api/v1/categorization-rules",
        headers=headers_b,
        json={
            "name": "Cross workspace account rule",
            "priority": 1,
            "category_id": category_a["id"],
            "account_id": account_a["id"],
        },
    )
    assert cross_account.status_code == 404, cross_account.text

    still_scoped = client.get(
        f"/api/v1/categorization-rules/{rule_a['id']}",
        headers=headers_b,
    )
    assert still_scoped.status_code == 404


async def _rule_set_version(identity: dict) -> int | None:
    async with AsyncSessionFactory() as session:
        return await session.scalar(
            select(CategorizationRuleSetControl.version).where(
                CategorizationRuleSetControl.workspace_id == uuid.UUID(identity["workspace"]["id"])
            )
        )


async def _context_for(identity: dict, session: AsyncSession) -> RequestContext:
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


async def _run_apply_against_committed_mutation(
    identity: dict,
    transaction_id: str,
    mutate: "Callable[[AsyncSession, RequestContext], Awaitable[None]]",
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Pause apply right after its optimistic preview, commit a rule-set mutation, then resume.

    The mutation commits before apply reaches its shared rule-set lock, so apply must observe the
    new deterministic ordering and refuse the stale proposal.
    """
    preview_finished = asyncio.Event()
    allow_apply_to_continue = asyncio.Event()
    original_preview = categorization_service.preview_transaction

    async def paused_preview(
        session: AsyncSession,
        workspace_id: uuid.UUID,
        selected_transaction_id: uuid.UUID,
    ) -> tuple[FinancialTransaction, categorization_service.CategorizationMatch | None]:
        result = await original_preview(session, workspace_id, selected_transaction_id)
        preview_finished.set()
        await allow_apply_to_continue.wait()
        return result

    monkeypatch.setattr(categorization_service, "preview_transaction", paused_preview)

    async with AsyncSessionFactory() as apply_session:
        apply_context = await _context_for(identity, apply_session)
        apply_task = asyncio.create_task(
            categorization_service.apply_to_transaction(
                apply_session,
                apply_context,
                uuid.UUID(transaction_id),
                1,
            )
        )
        await preview_finished.wait()
        async with AsyncSessionFactory() as mutation_session:
            await mutate(mutation_session, await _context_for(identity, mutation_session))
        allow_apply_to_continue.set()
        try:
            await apply_task
        except ApiError as exc:
            await apply_session.rollback()
            return exc.code
        raise AssertionError("Stale categorization proposal was applied")


def _assert_transaction_untouched(
    client: TestClient,
    headers: dict[str, str],
    transaction_id: str,
) -> None:
    unchanged = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["category"] is None
    assert unchanged.json()["version"] == 1
    assert asyncio.run(_categorization_audit(transaction_id)) is None


def _rule_set_race_fixture(
    client: TestClient,
    label: str,
) -> tuple[dict, dict[str, str], dict, dict, dict, str]:
    identity, headers = _register(client, label)
    account = _account(client, headers, f"{label} card")
    selected_category = _category(client, headers, f"{label} selected")
    intruder_category = _category(client, headers, f"{label} intruder")
    selected = _rule(
        client,
        headers,
        name=f"{label} selected rule",
        category_id=selected_category["id"],
        priority=50,
        counterparty_contains="race shop",
    )
    transaction_id = asyncio.run(
        _insert_uncategorized(identity, account["id"], counterparty="Race Shop statement")
    )
    return identity, headers, selected, selected_category, intruder_category, transaction_id


def test_apply_rejects_a_newly_created_higher_priority_rule(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers, _selected, _selected_category, intruder_category, transaction_id = (
        _rule_set_race_fixture(client, "Race create")
    )
    before = asyncio.run(_rule_set_version(identity))

    async def create_intruder(session: AsyncSession, context: RequestContext) -> None:
        await categorization_service.create_rule(
            session,
            context,
            CategorizationRuleCreate(
                name="Race create intruder",
                priority=1,
                category_id=uuid.UUID(intruder_category["id"]),
                counterparty_contains="race shop",
            ),
        )

    code = asyncio.run(
        _run_apply_against_committed_mutation(
            identity,
            transaction_id,
            create_intruder,
            monkeypatch,
        )
    )
    assert code == "CATEGORIZATION_RULE_CHANGED"
    _assert_transaction_untouched(client, headers, transaction_id)
    assert asyncio.run(_rule_set_version(identity)) == (before or 0) + 1


def test_apply_rejects_a_rule_reprioritized_above_the_selected_rule(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers, _selected, _selected_category, intruder_category, transaction_id = (
        _rule_set_race_fixture(client, "Race priority")
    )
    lower = _rule(
        client,
        headers,
        name="Race priority intruder",
        category_id=intruder_category["id"],
        priority=90,
        counterparty_contains="race shop",
    )
    before = asyncio.run(_rule_set_version(identity))

    async def reprioritize(session: AsyncSession, context: RequestContext) -> None:
        await categorization_service.update_rule(
            session,
            context,
            uuid.UUID(lower["id"]),
            CategorizationRuleUpdate(version=lower["version"], priority=1),
        )

    code = asyncio.run(
        _run_apply_against_committed_mutation(identity, transaction_id, reprioritize, monkeypatch)
    )
    assert code == "CATEGORIZATION_RULE_CHANGED"
    _assert_transaction_untouched(client, headers, transaction_id)
    assert asyncio.run(_rule_set_version(identity)) == (before or 0) + 1


def test_apply_rejects_a_restored_higher_priority_rule(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers, _selected, _selected_category, intruder_category, transaction_id = (
        _rule_set_race_fixture(client, "Race restore")
    )
    archived = _rule(
        client,
        headers,
        name="Race restore intruder",
        category_id=intruder_category["id"],
        priority=1,
        counterparty_contains="race shop",
    )
    archived_response = client.delete(
        f"/api/v1/categorization-rules/{archived['id']}?version={archived['version']}",
        headers=headers,
    )
    assert archived_response.status_code == 200, archived_response.text
    archived_version = archived_response.json()["version"]
    before = asyncio.run(_rule_set_version(identity))

    async def restore(session: AsyncSession, context: RequestContext) -> None:
        await categorization_service.restore_rule(
            session,
            context,
            uuid.UUID(archived["id"]),
            archived_version,
        )

    code = asyncio.run(
        _run_apply_against_committed_mutation(identity, transaction_id, restore, monkeypatch)
    )
    assert code == "CATEGORIZATION_RULE_CHANGED"
    _assert_transaction_untouched(client, headers, transaction_id)
    assert asyncio.run(_rule_set_version(identity)) == (before or 0) + 1


def test_apply_rejects_a_reactivated_higher_priority_rule(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers, _selected, _selected_category, intruder_category, transaction_id = (
        _rule_set_race_fixture(client, "Race activate")
    )
    inactive = _rule(
        client,
        headers,
        name="Race activate intruder",
        category_id=intruder_category["id"],
        priority=1,
        counterparty_contains="race shop",
        is_active=False,
    )
    before = asyncio.run(_rule_set_version(identity))

    async def activate(session: AsyncSession, context: RequestContext) -> None:
        await categorization_service.update_rule(
            session,
            context,
            uuid.UUID(inactive["id"]),
            CategorizationRuleUpdate(version=inactive["version"], is_active=True),
        )

    code = asyncio.run(
        _run_apply_against_committed_mutation(identity, transaction_id, activate, monkeypatch)
    )
    assert code == "CATEGORIZATION_RULE_CHANGED"
    _assert_transaction_untouched(client, headers, transaction_id)
    assert asyncio.run(_rule_set_version(identity)) == (before or 0) + 1


def test_rule_mutation_waits_for_an_apply_holding_the_shared_rule_set_lock(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other linearization arm: apply takes the shared lock first and wins.

    The competing rule creation must block on the exclusive rule-set gate until apply commits, and
    apply must complete using the rule it originally proposed.
    """
    identity, headers, selected, selected_category, intruder_category, transaction_id = (
        _rule_set_race_fixture(client, "Race barrier")
    )
    shared_lock_held = asyncio.Event()
    allow_apply_to_finish = asyncio.Event()
    # The proof and the mutation now live in the shared executor, so the barrier pauses inside it,
    # at the same instant as before: after the shared rule-set lock has been taken and while the
    # apply transaction is still open, so the competing rule mutation must wait for the commit.
    original_prepare = categorization_service.executor.prepare_rule_set

    async def paused_prepare(session, workspace_id, *, refresh: bool = False):
        prepared = await original_prepare(session, workspace_id, refresh=refresh)
        shared_lock_held.set()
        await allow_apply_to_finish.wait()
        return prepared

    monkeypatch.setattr(categorization_service.executor, "prepare_rule_set", paused_prepare)

    async def run_barrier() -> tuple[str, bool]:
        async with AsyncSessionFactory() as apply_session:
            apply_task = asyncio.create_task(
                categorization_service.apply_to_transaction(
                    apply_session,
                    await _context_for(identity, apply_session),
                    uuid.UUID(transaction_id),
                    1,
                )
            )
            await shared_lock_held.wait()
            async with AsyncSessionFactory() as mutation_session:
                mutation_task = asyncio.create_task(
                    categorization_service.create_rule(
                        mutation_session,
                        await _context_for(identity, mutation_session),
                        CategorizationRuleCreate(
                            name="Race barrier intruder",
                            priority=1,
                            category_id=uuid.UUID(intruder_category["id"]),
                            counterparty_contains="race shop",
                        ),
                    )
                )
                # The mutation must be parked on the exclusive rule-set gate, not racing ahead.
                await asyncio.sleep(0.5)
                blocked_while_apply_ran = not mutation_task.done()
                allow_apply_to_finish.set()
                result = await apply_task
                await mutation_task
            return result.match.rule.id if result.match else "", blocked_while_apply_ran

    applied_rule_id, blocked = asyncio.run(run_barrier())
    assert blocked is True
    assert str(applied_rule_id) == selected["id"]

    committed = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
    assert committed.status_code == 200, committed.text
    assert committed.json()["category"]["id"] == selected_category["id"]
    assert committed.json()["version"] == 2


def test_unrelated_transactions_categorize_concurrently_under_the_shared_lock(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Race parallel")
    account = _account(client, headers, "Parallel card")
    first_category = _category(client, headers, "Parallel first")
    second_category = _category(client, headers, "Parallel second")
    _rule(
        client,
        headers,
        name="Parallel first rule",
        category_id=first_category["id"],
        priority=10,
        counterparty_contains="alpha shop",
    )
    _rule(
        client,
        headers,
        name="Parallel second rule",
        category_id=second_category["id"],
        priority=20,
        counterparty_contains="beta shop",
    )
    first_id = asyncio.run(
        _insert_uncategorized(identity, account["id"], counterparty="Alpha Shop statement")
    )
    second_id = asyncio.run(
        _insert_uncategorized(identity, account["id"], counterparty="Beta Shop statement")
    )
    before = asyncio.run(_rule_set_version(identity))

    async def apply_one(transaction_id: str) -> bool:
        async with AsyncSessionFactory() as session:
            result = await categorization_service.apply_to_transaction(
                session,
                await _context_for(identity, session),
                uuid.UUID(transaction_id),
                1,
            )
            return result.applied

    async def run_parallel() -> list[bool]:
        return list(await asyncio.gather(apply_one(first_id), apply_one(second_id)))

    assert asyncio.run(run_parallel()) == [True, True]
    for transaction_id, expected in ((first_id, first_category), (second_id, second_category)):
        committed = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
        assert committed.status_code == 200, committed.text
        assert committed.json()["category"]["id"] == expected["id"]
    # Applying never mutates the rule set.
    assert asyncio.run(_rule_set_version(identity)) == before


def test_rule_set_revision_tracks_only_matching_relevant_mutations(client: TestClient) -> None:
    identity, headers = _register(client, "Revision semantics")
    category = _category(client, headers, "Revision target")
    # The migration backfills workspaces that already existed; a workspace created afterwards
    # materializes its control row lazily through get_or_create on first rule-set use, starting at
    # revision 1 for the empty rule set. Every bump below is relative to that starting point.
    assert asyncio.run(_rule_set_version(identity)) is None
    baseline = 1

    created = _rule(
        client,
        headers,
        name="Revision rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="revision shop",
    )
    assert asyncio.run(_rule_set_version(identity)) == baseline + 1

    renamed = client.patch(
        f"/api/v1/categorization-rules/{created['id']}",
        headers=headers,
        json={"version": created["version"], "name": "Revision rule renamed"},
    )
    assert renamed.status_code == 200, renamed.text
    # A rename cannot change matching or ordering: the rule version advances, the rule set does not.
    assert renamed.json()["version"] == created["version"] + 1
    assert asyncio.run(_rule_set_version(identity)) == baseline + 1

    reprioritized = client.patch(
        f"/api/v1/categorization-rules/{created['id']}",
        headers=headers,
        json={"version": renamed.json()["version"], "priority": 5},
    )
    assert reprioritized.status_code == 200, reprioritized.text
    assert asyncio.run(_rule_set_version(identity)) == baseline + 2

    idempotent = client.patch(
        f"/api/v1/categorization-rules/{created['id']}",
        headers=headers,
        json={"version": reprioritized.json()["version"], "priority": 5},
    )
    assert idempotent.status_code == 200, idempotent.text
    # Semantically empty PATCH: existing rule-version/audit contract is preserved, revision is not.
    assert asyncio.run(_rule_set_version(identity)) == baseline + 2

    archived = client.delete(
        f"/api/v1/categorization-rules/{created['id']}?version={idempotent.json()['version']}",
        headers=headers,
    )
    assert archived.status_code == 200, archived.text
    assert asyncio.run(_rule_set_version(identity)) == baseline + 3

    restored = client.post(
        f"/api/v1/categorization-rules/{created['id']}/restore",
        headers=headers,
        json={"version": archived.json()["version"]},
    )
    assert restored.status_code == 200, restored.text
    assert asyncio.run(_rule_set_version(identity)) == baseline + 4

    already_restored = client.post(
        f"/api/v1/categorization-rules/{created['id']}/restore",
        headers=headers,
        json={"version": restored.json()["version"]},
    )
    assert already_restored.status_code == 200, already_restored.text
    assert already_restored.json()["version"] == restored.json()["version"]
    assert asyncio.run(_rule_set_version(identity)) == baseline + 4


def test_rule_set_control_is_created_once_under_concurrent_first_use(client: TestClient) -> None:
    identity, _headers = _register(client, "Control bootstrap")
    workspace_id = uuid.UUID(identity["workspace"]["id"])

    async def drop_control() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                delete(CategorizationRuleSetControl).where(
                    CategorizationRuleSetControl.workspace_id == workspace_id
                )
            )
            await session.commit()

    asyncio.run(drop_control())
    assert asyncio.run(_rule_set_version(identity)) is None

    async def bootstrap() -> int:
        async with AsyncSessionFactory() as session:
            control = await categorization_repository.get_or_create_rule_set_control(
                session,
                workspace_id,
                for_update=True,
            )
            version = control.version
            await session.commit()
            return version

    async def run_bootstrap_race() -> list[int]:
        return list(await asyncio.gather(bootstrap(), bootstrap(), bootstrap()))

    assert asyncio.run(run_bootstrap_race()) == [1, 1, 1]

    async def control_rows() -> int:
        async with AsyncSessionFactory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(CategorizationRuleSetControl)
                    .where(CategorizationRuleSetControl.workspace_id == workspace_id)
                )
                or 0
            )

    assert asyncio.run(control_rows()) == 1
