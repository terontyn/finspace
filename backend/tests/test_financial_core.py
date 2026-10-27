import asyncio
import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.models.accounts import Account
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory


def _bootstrap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)
    response = client.post("/api/v1/dev/bootstrap")
    assert response.status_code == 200, response.text
    payload = response.json()
    return {
        "X-User-ID": payload["user_id"],
        "X-Workspace-ID": payload["workspace_id"],
    }


def _account_payload(
    name: str,
    *,
    currency: str = "RUB",
    account_type: str = "debit_card",
    opening_balance: str = "0.00",
) -> dict[str, str]:
    return {
        "name": name,
        "account_type": account_type,
        "currency": currency,
        "opening_balance": opening_balance,
        "opening_balance_at": "2026-01-01T00:00:00Z",
    }


def _create_account(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    **overrides: str,
) -> dict[str, object]:
    payload = _account_payload(name, **overrides)
    response = client.post("/api/v1/accounts", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_category(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    category_type: str,
    *,
    parent_id: str | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/categories",
        headers=headers,
        json={
            "name": name,
            "category_type": category_type,
            "parent_id": parent_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _transaction(
    client: TestClient,
    headers: dict[str, str],
    *,
    transaction_type: str,
    amount: str,
    account_id: str,
    category_id: str | None = None,
    target_account_id: str | None = None,
    status: str = "confirmed",
    related_transaction_id: str | None = None,
    splits: list[dict[str, str]] | None = None,
    comment: str | None = None,
) -> TestClient:
    return client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": "2026-07-01T12:00:00Z",
            "transaction_type": transaction_type,
            "amount": amount,
            "currency": "RUB",
            "account_id": account_id,
            "target_account_id": target_account_id,
            "category_id": category_id,
            "status": status,
            "source": "manual",
            "related_transaction_id": related_transaction_id,
            "splits": splits or [],
            "comment": comment,
        },
    )


def test_bootstrap_is_idempotent_and_forbidden_outside_development(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "environment", "test")
    assert client.post("/api/v1/dev/bootstrap").status_code == 404

    monkeypatch.setattr(settings, "environment", "development")
    first = client.post("/api/v1/dev/bootstrap")
    second = client.post("/api/v1/dev/bootstrap")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]
    assert first.json()["workspace_id"] == second.json()["workspace_id"]
    assert second.json()["created"] is False


def test_workspace_isolation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    first_headers = _bootstrap(client, monkeypatch)
    account = _create_account(client, first_headers, f"Isolation {uuid.uuid4().hex[:8]}")

    async def create_other_workspace() -> tuple[uuid.UUID, uuid.UUID]:
        async with AsyncSessionFactory() as session:
            other_user = User(
                email=f"other-{uuid.uuid4()}@finspace.local",
                normalized_email=f"other-{uuid.uuid4()}@finspace.local",
                display_name="Другой пользователь",
            )
            session.add(other_user)
            await session.flush()
            other_workspace = Workspace(
                name="Чужое пространство",
                base_currency="RUB",
                timezone="UTC",
                owner_user_id=other_user.id,
            )
            session.add(other_workspace)
            await session.flush()
            session.add(
                WorkspaceMember(
                    workspace_id=other_workspace.id,
                    user_id=other_user.id,
                    role="owner",
                )
            )
            await session.commit()
            return other_user.id, other_workspace.id

    user_id, workspace_id = asyncio.run(create_other_workspace())
    response = client.get(
        f"/api/v1/accounts/{account['id']}",
        headers={"X-User-ID": str(user_id), "X-Workspace-ID": str(workspace_id)},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_account_duplicate_optimistic_lock_and_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    request_id = str(uuid.uuid4())
    headers = {**headers, "X-Request-ID": request_id}
    name = f"Account {uuid.uuid4().hex[:8]}"
    account = _create_account(client, headers, name, opening_balance="125.50")

    duplicate = client.post("/api/v1/accounts", headers=headers, json=_account_payload(name))
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_NAME"

    updated = client.patch(
        f"/api/v1/accounts/{account['id']}",
        headers=headers,
        json={"version": account["version"], "description": "Тестовый счёт"},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    stale = client.patch(
        f"/api/v1/accounts/{account['id']}",
        headers=headers,
        json={"version": account["version"], "description": "Устаревшая запись"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    audit = client.get(
        f"/api/v1/audit?entity_type=account&entity_id={account['id']}",
        headers=headers,
    )
    assert audit.status_code == 200
    assert {item["action"] for item in audit.json()["items"]} >= {"create", "update"}
    assert any(item["request_id"] == request_id for item in audit.json()["items"])


def test_account_and_category_soft_delete_responses_can_be_restored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _create_account(client, headers, f"Lifecycle {uuid.uuid4().hex[:8]}")
    category = _create_category(
        client,
        headers,
        f"Lifecycle {uuid.uuid4().hex[:8]}",
        "expense",
    )

    deleted_account = client.delete(
        f"/api/v1/accounts/{account['id']}?version={account['version']}",
        headers=headers,
    )
    assert deleted_account.status_code == 200, deleted_account.text
    assert deleted_account.json()["version"] == int(account["version"]) + 1

    restored_account = client.post(
        f"/api/v1/accounts/{account['id']}/restore",
        headers=headers,
        json={"version": deleted_account.json()["version"]},
    )
    assert restored_account.status_code == 200, restored_account.text
    assert restored_account.json()["version"] == int(account["version"]) + 2

    deleted_category = client.delete(
        f"/api/v1/categories/{category['id']}?version={category['version']}",
        headers=headers,
    )
    assert deleted_category.status_code == 200, deleted_category.text
    assert deleted_category.json()["version"] == int(category["version"]) + 1

    restored_category = client.post(
        f"/api/v1/categories/{category['id']}/restore",
        headers=headers,
        json={"version": deleted_category.json()["version"]},
    )
    assert restored_category.status_code == 200, restored_category.text
    assert restored_category.json()["version"] == int(category["version"]) + 2


def test_account_and_category_mutation_responses_survive_commit_expiration(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    AsyncSessionFactory.configure(expire_on_commit=True)
    try:
        account = _create_account(
            client,
            headers,
            f"Expiring lifecycle {uuid.uuid4().hex[:8]}",
        )
        parent = _create_category(
            client,
            headers,
            f"Expiring parent {uuid.uuid4().hex[:8]}",
            "expense",
        )
        child = _create_category(
            client,
            headers,
            f"Expiring child {uuid.uuid4().hex[:8]}",
            "expense",
            parent_id=str(parent["id"]),
        )

        archived_account = client.patch(
            f"/api/v1/accounts/{account['id']}",
            headers=headers,
            json={"version": account["version"], "is_archived": True},
        )
        assert archived_account.status_code == 200, archived_account.text
        assert archived_account.json()["is_archived"] is True
        assert datetime.fromisoformat(archived_account.json()["updated_at"])

        deleted_account = client.delete(
            f"/api/v1/accounts/{account['id']}?version={archived_account.json()['version']}",
            headers=headers,
        )
        assert deleted_account.status_code == 200, deleted_account.text
        assert datetime.fromisoformat(deleted_account.json()["updated_at"])
        assert client.get(f"/api/v1/accounts/{account['id']}", headers=headers).status_code == 404

        repeated_account_delete = client.delete(
            f"/api/v1/accounts/{account['id']}?version={deleted_account.json()['version']}",
            headers=headers,
        )
        assert repeated_account_delete.status_code == 404
        assert repeated_account_delete.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"

        restored_account = client.post(
            f"/api/v1/accounts/{account['id']}/restore",
            headers=headers,
            json={"version": deleted_account.json()["version"]},
        )
        assert restored_account.status_code == 200, restored_account.text
        assert restored_account.json()["version"] == int(deleted_account.json()["version"]) + 1
        assert datetime.fromisoformat(restored_account.json()["updated_at"])

        archived_category = client.patch(
            f"/api/v1/categories/{parent['id']}",
            headers=headers,
            json={"version": parent["version"], "is_archived": True},
        )
        assert archived_category.status_code == 200, archived_category.text
        assert archived_category.json()["is_archived"] is True

        deleted_category = client.delete(
            f"/api/v1/categories/{parent['id']}?version={archived_category.json()['version']}",
            headers=headers,
        )
        assert deleted_category.status_code == 200, deleted_category.text
        assert datetime.fromisoformat(deleted_category.json()["updated_at"])
        repeated_category_delete = client.delete(
            f"/api/v1/categories/{parent['id']}?version={deleted_category.json()['version']}",
            headers=headers,
        )
        assert repeated_category_delete.status_code == 404
        assert repeated_category_delete.json()["error"]["code"] == "CATEGORY_NOT_FOUND"
        child_after_parent_delete = client.get(f"/api/v1/categories/{child['id']}", headers=headers)
        assert child_after_parent_delete.status_code == 200
        assert child_after_parent_delete.json()["parent_id"] == parent["id"]

        restored_category = client.post(
            f"/api/v1/categories/{parent['id']}/restore",
            headers=headers,
            json={"version": deleted_category.json()["version"]},
        )
        assert restored_category.status_code == 200, restored_category.text
        assert restored_category.json()["version"] == int(deleted_category.json()["version"]) + 1
        assert datetime.fromisoformat(restored_category.json()["updated_at"])
    finally:
        AsyncSessionFactory.configure(expire_on_commit=False)


def test_category_create_tree_and_cycle(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    parent = _create_category(client, headers, f"Parent {uuid.uuid4().hex[:8]}", "expense")
    child = _create_category(
        client,
        headers,
        f"Child {uuid.uuid4().hex[:8]}",
        "expense",
        parent_id=str(parent["id"]),
    )

    cycle = client.patch(
        f"/api/v1/categories/{parent['id']}",
        headers=headers,
        json={"version": parent["version"], "parent_id": child["id"]},
    )
    assert cycle.status_code == 422
    assert cycle.json()["error"]["code"] == "VALIDATION_ERROR"

    tree = client.get("/api/v1/categories/tree", headers=headers)
    assert tree.status_code == 200
    assert any(
        node["id"] == parent["id"] and any(item["id"] == child["id"] for item in node["children"])
        for node in tree.json()
    )


def test_income_expense_transfer_balances_and_summary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    source = _create_account(client, headers, f"Source {uuid.uuid4().hex[:8]}")
    target = _create_account(client, headers, f"Target {uuid.uuid4().hex[:8]}")
    euro = _create_account(client, headers, f"Euro {uuid.uuid4().hex[:8]}", currency="EUR")
    income_category = _create_category(client, headers, f"Income {uuid.uuid4().hex[:8]}", "income")
    expense_category = _create_category(
        client, headers, f"Expense {uuid.uuid4().hex[:8]}", "expense"
    )

    income = _transaction(
        client,
        headers,
        transaction_type="income",
        amount="1000.00",
        account_id=str(source["id"]),
        category_id=str(income_category["id"]),
    )
    expense = _transaction(
        client,
        headers,
        transaction_type="expense",
        amount="250.00",
        account_id=str(source["id"]),
        category_id=str(expense_category["id"]),
    )
    transfer = _transaction(
        client,
        headers,
        transaction_type="transfer",
        amount="300.00",
        account_id=str(source["id"]),
        target_account_id=str(target["id"]),
    )
    assert income.status_code == expense.status_code == transfer.status_code == 201

    invalid_currency = _transaction(
        client,
        headers,
        transaction_type="transfer",
        amount="10.00",
        account_id=str(source["id"]),
        target_account_id=str(euro["id"]),
    )
    assert invalid_currency.status_code == 422
    assert invalid_currency.json()["error"]["code"] == "INVALID_TRANSFER"

    balances = client.get("/api/v1/accounts/balances", headers=headers).json()
    by_id = {item["account_id"]: item["balance"] for item in balances}
    assert by_id[str(source["id"])] == "450.0000"
    assert by_id[str(target["id"])] == "300.0000"

    summary = client.get("/api/v1/financial-summary", headers=headers).json()
    rub = next(item for item in summary["groups"] if item["currency"] == "RUB")
    assert rub["income"] == "1000.0000"
    assert rub["expense"] == "250.0000"
    assert rub["net_cashflow"] == "750.0000"
    assert rub["transfer_volume"] == "300.0000"


def test_draft_cancelled_and_soft_deleted_do_not_affect_balance(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _create_account(client, headers, f"States {uuid.uuid4().hex[:8]}")
    category = _create_category(client, headers, f"State income {uuid.uuid4().hex[:8]}", "income")
    draft = _transaction(
        client,
        headers,
        transaction_type="income",
        amount="100.00",
        account_id=str(account["id"]),
        category_id=str(category["id"]),
        status="draft",
    )
    confirmed = _transaction(
        client,
        headers,
        transaction_type="income",
        amount="50.00",
        account_id=str(account["id"]),
        category_id=str(category["id"]),
    ).json()
    deleted = _transaction(
        client,
        headers,
        transaction_type="income",
        amount="40.00",
        account_id=str(account["id"]),
        category_id=str(category["id"]),
    ).json()
    assert draft.status_code == 201
    assert (
        client.post(
            f"/api/v1/transactions/{confirmed['id']}/cancel",
            headers=headers,
            json={"version": confirmed["version"]},
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/v1/transactions/{deleted['id']}?version={deleted['version']}",
            headers=headers,
        ).status_code
        == 200
    )
    balances = client.get("/api/v1/accounts/balances", headers=headers).json()
    balance = next(item for item in balances if item["account_id"] == account["id"])
    assert balance["balance"] == "0.0000"


def test_split_rules(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _create_account(client, headers, f"Splits {uuid.uuid4().hex[:8]}")
    first = _create_category(client, headers, f"Split A {uuid.uuid4().hex[:8]}", "expense")
    second = _create_category(client, headers, f"Split B {uuid.uuid4().hex[:8]}", "expense")

    valid = _transaction(
        client,
        headers,
        transaction_type="expense",
        amount="30.00",
        account_id=str(account["id"]),
        splits=[
            {"category_id": str(first["id"]), "amount": "10.00"},
            {"category_id": str(second["id"]), "amount": "20.00"},
        ],
    )
    assert valid.status_code == 201
    assert len(valid.json()["splits"]) == 2

    invalid = _transaction(
        client,
        headers,
        transaction_type="expense",
        amount="30.00",
        account_id=str(account["id"]),
        splits=[{"category_id": str(first["id"]), "amount": "29.99"}],
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_SPLIT_TOTAL"

    target = _create_account(client, headers, f"Split target {uuid.uuid4().hex[:8]}")
    transfer_split = _transaction(
        client,
        headers,
        transaction_type="transfer",
        amount="10.00",
        account_id=str(account["id"]),
        target_account_id=str(target["id"]),
        splits=[{"category_id": str(first["id"]), "amount": "10.00"}],
    )
    assert transfer_split.status_code == 422
    assert transfer_split.json()["error"]["code"] == "INVALID_TRANSFER"


def test_partial_refund_and_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _create_account(client, headers, f"Refund {uuid.uuid4().hex[:8]}")
    category = _create_category(
        client, headers, f"Refund expense {uuid.uuid4().hex[:8]}", "expense"
    )
    expense = _transaction(
        client,
        headers,
        transaction_type="expense",
        amount="100.00",
        account_id=str(account["id"]),
        category_id=str(category["id"]),
    ).json()
    refund = _transaction(
        client,
        headers,
        transaction_type="refund",
        amount="40.00",
        account_id=str(account["id"]),
        related_transaction_id=str(expense["id"]),
    )
    assert refund.status_code == 201

    excessive = _transaction(
        client,
        headers,
        transaction_type="refund",
        amount="60.01",
        account_id=str(account["id"]),
        related_transaction_id=str(expense["id"]),
    )
    assert excessive.status_code == 422
    assert excessive.json()["error"]["code"] == "REFUND_LIMIT_EXCEEDED"

    balances = client.get("/api/v1/accounts/balances", headers=headers).json()
    balance = next(item for item in balances if item["account_id"] == account["id"])
    assert balance["balance"] == "-60.0000"


def test_refund_calculations_do_not_follow_related_transaction_across_workspaces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _create_account(
        client,
        headers,
        f"Refund isolation {uuid.uuid4().hex[:8]}",
        opening_balance="100.00",
    )
    category = _create_category(
        client, headers, f"Refund isolation expense {uuid.uuid4().hex[:8]}", "expense"
    )
    expense = _transaction(
        client,
        headers,
        transaction_type="expense",
        amount="10.00",
        account_id=str(account["id"]),
        category_id=str(category["id"]),
    ).json()
    refund = _transaction(
        client,
        headers,
        transaction_type="refund",
        amount="5.00",
        account_id=str(account["id"]),
        related_transaction_id=str(expense["id"]),
    ).json()
    before_summary = client.get("/api/v1/financial-summary", headers=headers).json()
    before_rub = next(item for item in before_summary["groups"] if item["currency"] == "RUB")
    before_expense = Decimal(before_rub["expense"])

    async def corrupt_reference_to_foreign_transaction() -> None:
        async with AsyncSessionFactory() as session:
            unique = uuid.uuid4().hex
            other_user = User(
                email=f"refund-isolation-{unique}@test.local",
                normalized_email=f"refund-isolation-{unique}@test.local",
                display_name="Refund isolation owner",
            )
            session.add(other_user)
            await session.flush()
            other_workspace = Workspace(
                name=f"Refund isolation {unique[:8]}",
                base_currency="RUB",
                timezone="UTC",
                owner_user_id=other_user.id,
            )
            session.add(other_workspace)
            await session.flush()
            session.add(
                WorkspaceMember(
                    workspace_id=other_workspace.id,
                    user_id=other_user.id,
                    role="owner",
                )
            )
            other_account = Account(
                workspace_id=other_workspace.id,
                name=f"Foreign {unique[:8]}",
                account_type="debit_card",
                currency="RUB",
                opening_balance=Decimal("0"),
                opening_balance_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            )
            session.add(other_account)
            await session.flush()
            foreign_original = FinancialTransaction(
                workspace_id=other_workspace.id,
                occurred_at=datetime.fromisoformat("2026-07-01T12:00:00+00:00"),
                transaction_type="expense",
                amount=Decimal("1"),
                currency="RUB",
                account_id=other_account.id,
                status="confirmed",
                source="manual",
                created_by=other_user.id,
                updated_by=other_user.id,
            )
            session.add(foreign_original)
            await session.flush()
            current_refund = await session.get(FinancialTransaction, uuid.UUID(str(refund["id"])))
            assert current_refund is not None
            current_refund.related_transaction_id = foreign_original.id
            await session.commit()

    asyncio.run(corrupt_reference_to_foreign_transaction())

    balances = client.get("/api/v1/accounts/balances", headers=headers).json()
    balance = next(item for item in balances if item["account_id"] == account["id"])
    assert balance["balance"] == "90.0000"

    summary = client.get("/api/v1/financial-summary", headers=headers).json()
    rub = next(item for item in summary["groups"] if item["currency"] == "RUB")
    assert Decimal(rub["expense"]) == before_expense + Decimal("5.0000")


def test_money_is_string_in_api_and_decimal_in_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _create_account(
        client,
        headers,
        f"Decimal {uuid.uuid4().hex[:8]}",
        opening_balance="0.1234",
    )
    assert account["opening_balance"] == "0.1234"

    async def database_value() -> Decimal:
        async with AsyncSessionFactory() as session:
            value = await session.scalar(
                select(Account.opening_balance).where(Account.id == uuid.UUID(str(account["id"])))
            )
            assert value is not None
            return value

    assert isinstance(asyncio.run(database_value()), Decimal)

    float_payload = _account_payload(f"Float {uuid.uuid4().hex[:8]}")
    float_payload["opening_balance"] = 0.1  # type: ignore[assignment]
    response = client.post("/api/v1/accounts", headers=headers, json=float_payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
