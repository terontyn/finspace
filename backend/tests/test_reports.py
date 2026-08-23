import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory


def _bootstrap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)

    async def create_identity() -> dict[str, str]:
        async with AsyncSessionFactory() as session:
            unique = uuid.uuid4().hex
            user = User(
                email=f"reports-{unique}@test.local",
                normalized_email=f"reports-{unique}@test.local",
                display_name="Reports owner",
                timezone="UTC",
            )
            session.add(user)
            await session.flush()
            workspace = Workspace(
                name=f"Reports {unique[:8]}",
                base_currency="RUB",
                timezone="Asia/Yekaterinburg",
                owner_user_id=user.id,
            )
            session.add(workspace)
            await session.flush()
            session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
            await session.commit()
            return {
                "X-User-ID": str(user.id),
                "X-Workspace-ID": str(workspace.id),
            }

    return asyncio.run(create_identity())


def _account(
    client: TestClient, headers: dict[str, str], *, name: str, currency: str
) -> dict[str, object]:
    response = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": name,
            "account_type": "debit_card",
            "currency": currency,
            "opening_balance": "0.0000",
            "opening_balance_at": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _category(
    client: TestClient, headers: dict[str, str], *, name: str, category_type: str
) -> dict[str, object]:
    response = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": name, "category_type": category_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _transaction(
    client: TestClient,
    headers: dict[str, str],
    *,
    account_id: str,
    amount: str,
    currency: str,
    occurred_at: str,
    transaction_type: str,
    category_id: str | None = None,
    target_account_id: str | None = None,
    related_transaction_id: str | None = None,
    status: str = "confirmed",
    comment: str | None = None,
    counterparty: str | None = None,
    splits: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": occurred_at,
            "transaction_type": transaction_type,
            "amount": amount,
            "currency": currency,
            "account_id": account_id,
            "target_account_id": target_account_id,
            "category_id": category_id,
            "counterparty": counterparty,
            "comment": comment,
            "related_transaction_id": related_transaction_id,
            "status": status,
            "source": "manual",
            "splits": splits or [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_financial_report_keeps_currencies_separate_and_excludes_transfers_from_flow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    rub_source = _account(client, headers, name="RUB source", currency="RUB")
    rub_target = _account(client, headers, name="RUB target", currency="RUB")
    usd_account = _account(client, headers, name="USD", currency="USD")
    rub_income = _category(client, headers, name="Salary", category_type="income")
    rub_expense = _category(client, headers, name="Food", category_type="expense")
    usd_income = _category(client, headers, name="USD income", category_type="income")
    usd_expense = _category(client, headers, name="USD expense", category_type="expense")

    _transaction(
        client,
        headers,
        account_id=str(rub_source["id"]),
        amount="100",
        currency="RUB",
        occurred_at="2026-07-15T12:00:00Z",
        transaction_type="income",
        category_id=str(rub_income["id"]),
    )
    _transaction(
        client,
        headers,
        account_id=str(rub_source["id"]),
        amount="250",
        currency="RUB",
        occurred_at="2026-08-10T12:00:00Z",
        transaction_type="income",
        category_id=str(rub_income["id"]),
        counterparty="Employer",
    )
    expense = _transaction(
        client,
        headers,
        account_id=str(rub_source["id"]),
        amount="100",
        currency="RUB",
        occurred_at="2026-08-11T12:00:00Z",
        transaction_type="expense",
        category_id=str(rub_expense["id"]),
        counterparty="Known merchant",
    )
    _transaction(
        client,
        headers,
        account_id=str(rub_source["id"]),
        target_account_id=str(rub_target["id"]),
        amount="50",
        currency="RUB",
        occurred_at="2026-08-12T12:00:00Z",
        transaction_type="transfer",
    )
    _transaction(
        client,
        headers,
        account_id=str(usd_account["id"]),
        amount="40",
        currency="USD",
        occurred_at="2026-08-13T12:00:00Z",
        transaction_type="income",
        category_id=str(usd_income["id"]),
    )
    _transaction(
        client,
        headers,
        account_id=str(usd_account["id"]),
        amount="10",
        currency="USD",
        occurred_at="2026-08-14T12:00:00Z",
        transaction_type="expense",
        category_id=str(usd_expense["id"]),
    )

    response = client.get(
        "/api/v1/reports/financial",
        headers=headers,
        params={"date_from": "2026-08-01", "date_to": "2026-08-31"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["period"]["timezone"] == "Asia/Yekaterinburg"
    groups = {item["currency"]: item for item in payload["groups"]}
    assert set(groups) == {"RUB", "USD"}
    assert (
        groups["RUB"]
        | {
            "income": "250.0000",
            "expense": "100.0000",
            "net_cashflow": "150.0000",
            "transfer_volume": "50.0000",
        }
        == groups["RUB"]
    )
    assert groups["USD"]["income"] == "40.0000"
    assert groups["USD"]["expense"] == "10.0000"
    assert groups["USD"]["net_cashflow"] == "30.0000"
    assert groups["RUB"]["spending_by_category"] == [
        {
            "category_id": rub_expense["id"],
            "name": "Food",
            "amount": "100.0000",
            "transaction_count": 1,
        }
    ]
    assert groups["RUB"]["largest_expenses"][0]["transaction_id"] == expense["id"]
    months = {item["month"]: item for item in groups["RUB"]["monthly_comparison"]}
    assert months["2026-07"]["income"] == "100.0000"
    assert months["2026-08"]["net_cashflow"] == "150.0000"

    filtered = client.get(
        "/api/v1/reports/financial",
        headers=headers,
        params={
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "currency": "USD",
        },
    )
    assert filtered.status_code == 200
    assert [item["currency"] for item in filtered.json()["groups"]] == ["USD"]


def test_report_refunds_splits_adjustment_timezone_permissions_and_validation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers, name="RUB", currency="RUB")
    food = _category(client, headers, name="Food", category_type="expense")
    travel = _category(client, headers, name="Travel", category_type="expense")
    expense = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        amount="100",
        currency="RUB",
        occurred_at="2026-03-08T12:00:00Z",
        transaction_type="expense",
        counterparty="Split merchant",
        splits=[
            {"category_id": str(food["id"]), "amount": "60"},
            {"category_id": str(travel["id"]), "amount": "40"},
        ],
    )
    _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        amount="25",
        currency="RUB",
        occurred_at="2026-03-08T13:00:00Z",
        transaction_type="refund",
        related_transaction_id=str(expense["id"]),
    )
    _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        amount="5",
        currency="RUB",
        occurred_at="2026-03-08T14:00:00Z",
        transaction_type="adjustment",
        comment="Explicit correction",
    )
    _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        amount="10",
        currency="RUB",
        occurred_at="2026-03-08T18:59:59Z",
        transaction_type="adjustment",
        comment="Before local cutoff",
    )
    _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        amount="99",
        currency="RUB",
        occurred_at="2026-03-08T19:00:00Z",
        transaction_type="adjustment",
        comment="At next local day",
    )

    response = client.get(
        "/api/v1/reports/financial",
        headers=headers,
        params={"date_from": "2026-03-08", "date_to": "2026-03-08"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["period"]["cutoff_to"] == "2026-03-08T19:00:00Z"
    group = payload["groups"][0]
    assert group["expense"] == "75.0000"
    assert group["adjustment"] == "15.0000"
    assert group["net_cashflow"] == "-60.0000"
    categories = {item["name"]: item["amount"] for item in group["spending_by_category"]}
    assert categories == {"Food": "45.0000", "Travel": "30.0000"}

    async def viewer_headers() -> dict[str, str]:
        async with AsyncSessionFactory() as session:
            unique = uuid.uuid4().hex
            viewer = User(
                email=f"report-viewer-{unique}@test.local",
                normalized_email=f"report-viewer-{unique}@test.local",
                display_name="Report viewer",
            )
            session.add(viewer)
            await session.flush()
            session.add(
                WorkspaceMember(
                    workspace_id=uuid.UUID(headers["X-Workspace-ID"]),
                    user_id=viewer.id,
                    role="viewer",
                )
            )
            await session.commit()
            return {
                "X-User-ID": str(viewer.id),
                "X-Workspace-ID": headers["X-Workspace-ID"],
            }

    viewer_response = client.get(
        "/api/v1/reports/financial",
        headers=asyncio.run(viewer_headers()),
        params={"date_from": "2026-03-08", "date_to": "2026-03-08"},
    )
    assert viewer_response.status_code == 200

    invalid = client.get(
        "/api/v1/reports/financial",
        headers=headers,
        params={"date_from": "2026-04-01", "date_to": "2026-03-01"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_REPORT_PERIOD"
