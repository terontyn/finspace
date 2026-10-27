import asyncio
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.audit import AuditLog
from app.db.models.automations import MonthCloseRevision
from app.db.models.budgets import BudgetAllocation, BudgetPeriod, BudgetPlanRevision
from app.db.models.google_sync import SyncOutbox
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.dependencies.context import RequestContext
from app.main import app
from app.schemas.budgets import BudgetGroupResponse, BudgetUpsertRequest
from app.services import budgets as budget_service
from app.services import month_close as month_close_service
from app.services.financial_period_guard import period_bounds
from tests.test_automations import _register, _service_key


@pytest.fixture(autouse=True)
def _configure_budget_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _account(
    client: TestClient,
    headers: dict[str, str],
    *,
    currency: str = "RUB",
    name: str | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": name or f"Budget {currency} {uuid.uuid4().hex[:8]}",
            "account_type": "cash",
            "currency": currency,
            "opening_balance": "0.0000",
            "opening_balance_at": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _category(
    client: TestClient,
    headers: dict[str, str],
    category_type: str = "expense",
    *,
    parent_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/categories",
        headers=headers,
        json={
            "name": name or f"Budget category {uuid.uuid4().hex[:8]}",
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
    account_id: str,
    transaction_type: str,
    amount: str,
    currency: str = "RUB",
    occurred_at: str = "2026-07-10T10:00:00Z",
    category_id: str | None = None,
    target_account_id: str | None = None,
    related_transaction_id: str | None = None,
    status: str = "confirmed",
    splits: list[dict[str, str]] | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
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
            "related_transaction_id": related_transaction_id,
            "status": status,
            "source": "manual",
            "splits": splits or [],
            "comment": comment,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _budget(
    client: TestClient,
    headers: dict[str, str],
    period: str,
    currency: str,
    *,
    planned_income: str = "0.0000",
    rollover_policy: str = "none",
    allocations: list[dict[str, object]] | None = None,
    version: int | None = None,
    key: str | None = None,
) -> Response:
    return client.put(
        f"/api/v1/budgets/{period}/{currency}",
        headers={**headers, "X-Idempotency-Key": key or f"budget-{uuid.uuid4()}"},
        json={
            "version": version,
            "planned_income": planned_income,
            "rollover_policy": rollover_policy,
            "allocations": allocations or [],
        },
    )


def _prepare(client: TestClient, headers: dict[str, str], period: str) -> dict[str, Any]:
    year, month = period.split("-")
    response = client.post(
        f"/api/v1/month-close/{year}/{int(month)}/prepare", headers=headers, json={}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _confirm(
    client: TestClient,
    headers: dict[str, str],
    period: str,
    prepared: dict[str, Any],
) -> dict[str, Any]:
    year, month = period.split("-")
    response = client.post(
        f"/api/v1/month-close/{year}/{int(month)}/confirm",
        headers={**headers, "X-Idempotency-Key": f"close-{uuid.uuid4()}"},
        json={
            "version": prepared["version"],
            "confirm": True,
            "prepare_token": prepared["prepare_token"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_period_parsing_and_timezone_half_open_bounds() -> None:
    assert budget_service.parse_period("2026-07") == date(2026, 7, 1)
    for invalid in ("2026-7", "2026-02-01", "1999-12", "2026-13", "not-a-month"):
        with pytest.raises(ApiError) as error:
            budget_service.parse_period(invalid)
        assert error.value.code == "BUDGET_PERIOD_INVALID"

    start, end = period_bounds(date(2026, 3, 1), "Europe/Berlin")
    assert start.astimezone(ZoneInfo("Europe/Berlin")).isoformat().startswith("2026-03-01T00:00")
    assert end.astimezone(ZoneInfo("Europe/Berlin")).isoformat().startswith("2026-04-01T00:00")
    assert (end - start).total_seconds() == 743 * 3600

    assert budget_service._apply_rollover("none", Decimal("25")) == Decimal("0.0000")
    assert budget_service._apply_rollover("positive_only", Decimal("25")) == Decimal("25.0000")
    assert budget_service._apply_rollover("positive_only", Decimal("-5")) == Decimal("0.0000")
    assert budget_service._apply_rollover("full", Decimal("25")) == Decimal("25.0000")
    assert budget_service._apply_rollover("full", Decimal("-5")) == Decimal("-5.0000")
    assert budget_service._usage_percent(Decimal("0"), Decimal("0")) is None
    usage = budget_service._usage_percent(Decimal("1"), Decimal("3"))
    assert usage == Decimal("33.3333")
    assert usage.is_finite()

    schema_paths = app.openapi()["paths"]
    expected_operations = {
        ("/api/v1/budgets/{period}", "get"),
        ("/api/v1/budgets/{period}/{currency}", "get"),
        ("/api/v1/budgets/{period}/{currency}", "put"),
        ("/api/v1/budgets/{period}/{currency}", "delete"),
        ("/api/v1/budgets/{period}/{currency}/restore", "post"),
        ("/api/v1/budgets/{period}/{currency}/copy", "post"),
        ("/api/v1/budgets/{period}/{currency}/history", "get"),
    }
    operation_ids = [
        schema_paths[path][method]["operationId"] for path, method in expected_operations
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_canonical_budget_snapshot_is_stable_and_allocation_sorted() -> None:
    workspace_id = uuid.UUID(int=1)
    user_id = uuid.UUID(int=2)
    budget = BudgetPeriod(
        id=uuid.UUID(int=3),
        workspace_id=workspace_id,
        period_month=date(2026, 7, 1),
        currency="RUB",
        planned_income=Decimal("12.3"),
        rollover_policy="full",
        version=4,
        created_by=user_id,
        updated_by=user_id,
    )
    first = BudgetAllocation(
        id=uuid.UUID(int=10),
        budget_period_id=budget.id,
        category_id=uuid.UUID(int=100),
        planned_amount=Decimal("1.2"),
        note=None,
    )
    second = BudgetAllocation(
        id=uuid.UUID(int=11),
        budget_period_id=budget.id,
        category_id=uuid.UUID(int=200),
        planned_amount=Decimal("2.3456"),
        note="stable",
    )
    forward = budget_service.budget_plan_snapshot(budget, [first, second])
    reverse = budget_service.budget_plan_snapshot(budget, [second, first])
    assert forward == reverse
    assert forward["period"] == "2026-07"
    assert forward["planned_income"] == "12.3000"
    allocations = cast(list[dict[str, object]], forward["allocations"])
    assert [item["category_id"] for item in allocations] == [
        str(first.category_id),
        str(second.category_id),
    ]
    assert [item["planned_amount"] for item in allocations] == [
        "1.2000",
        "2.3456",
    ]


def test_budget_migration_does_not_change_global_audit_contract() -> None:
    migration = (
        Path(__file__).parents[1] / "alembic/versions/0009_budget_planning_core.py"
    ).read_text(encoding="utf-8")
    assert "ck_audit_log_action" not in migration
    assert "DELETE FROM audit_log" not in migration


def test_actual_projection_exact_categories_refunds_statuses_and_currency(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Budget actuals")
    rub = _account(client, headers)
    rub_target = _account(client, headers, name="Budget transfer target")
    usd = _account(client, headers, currency="USD")
    parent = _category(client, headers, name="Food")
    child = _category(client, headers, parent_id=str(parent["id"]), name="Cafe")
    unbudgeted = _category(client, headers, name="Other")
    income_category = _category(client, headers, "income", name="Salary")

    income = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="income",
        amount="200",
        category_id=str(income_category["id"]),
    )
    _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="refund",
        amount="20",
        related_transaction_id=str(income["id"]),
    )
    _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="expense",
        amount="40",
        category_id=str(parent["id"]),
    )
    split = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="expense",
        amount="30",
        splits=[
            {"category_id": str(parent["id"]), "amount": "10"},
            {"category_id": str(child["id"]), "amount": "20"},
        ],
    )
    _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="refund",
        amount="9",
        related_transaction_id=str(split["id"]),
    )
    rounding_split = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="expense",
        amount="3",
        splits=[
            {"category_id": str(parent["id"]), "amount": "1"},
            {"category_id": str(child["id"]), "amount": "2"},
        ],
    )
    _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="refund",
        amount="1",
        related_transaction_id=str(rounding_split["id"]),
    )
    june = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="expense",
        amount="5",
        category_id=str(parent["id"]),
        occurred_at="2026-06-10T10:00:00Z",
    )
    _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="refund",
        amount="2",
        related_transaction_id=str(june["id"]),
    )
    _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="expense",
        amount="4",
        category_id=str(unbudgeted["id"]),
    )
    reconciled = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="expense",
        amount="2",
        category_id=str(parent["id"]),
    )

    async def mark_reconciled() -> None:
        async with AsyncSessionFactory() as session:
            row = await session.get(FinancialTransaction, uuid.UUID(str(reconciled["id"])))
            assert row is not None
            row.status = "reconciled"
            await session.commit()

    asyncio.run(mark_reconciled())
    _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="adjustment",
        amount="5",
        comment="Opening correction",
    )
    _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        target_account_id=str(rub_target["id"]),
        transaction_type="transfer",
        amount="50",
    )
    for status in ("draft", "cancelled"):
        _transaction(
            client,
            headers,
            account_id=str(rub["id"]),
            transaction_type="expense",
            amount="100",
            category_id=str(parent["id"]),
            status=status,
        )
    deleted = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        transaction_type="expense",
        amount="7",
        category_id=str(parent["id"]),
    )
    removed = client.delete(
        f"/api/v1/transactions/{deleted['id']}?version={deleted['version']}", headers=headers
    )
    assert removed.status_code == 200, removed.text
    _transaction(
        client,
        headers,
        account_id=str(usd["id"]),
        transaction_type="expense",
        amount="3",
        currency="USD",
        category_id=str(parent["id"]),
    )

    rub_budget = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        planned_income="200",
        allocations=[
            {"category_id": parent["id"], "planned_amount": "100"},
            {"category_id": child["id"], "planned_amount": "50"},
        ],
    )
    usd_budget = _budget(
        client,
        headers,
        "2026-07",
        "USD",
        planned_income="50",
        allocations=[{"category_id": parent["id"], "planned_amount": "20"}],
    )
    assert rub_budget.status_code == usd_budget.status_code == 200

    month = client.get("/api/v1/budgets/2026-07", headers=headers)
    assert month.status_code == 200, month.text
    groups = {item["currency"]: item for item in month.json()["groups"]}
    assert set(groups) == {"RUB", "USD"}
    actual = groups["RUB"]
    assert actual["actual_income"] == "180.0000"
    assert actual["actual_expense"] == "67.0000"
    assert actual["adjustment"] == "5.0000"
    assert actual["actual_net_cashflow"] == "118.0000"
    assert actual["budgeted_actual_expense"] == "63.0000"
    assert actual["unbudgeted_actual_expense"] == "4.0000"
    assert actual["remaining"] == "83.0000"
    assert actual["unallocated"] == "50.0000"
    by_category = {item["category_id"]: item for item in actual["allocations"]}
    assert by_category[str(parent["id"])]["actual"] == "47.6667"
    assert by_category[str(child["id"])]["actual"] == "15.3333"
    assert by_category[str(parent["id"])]["usage_percent"] == "47.6667"
    assert by_category[str(child["id"])]["usage_percent"] == "30.6666"
    assert groups["USD"]["actual_expense"] == "3.0000"
    assert groups["USD"]["budgeted_actual_expense"] == "3.0000"
    assert "NaN" not in month.text and "Infinity" not in month.text

    report = client.get(
        "/api/v1/reports/financial",
        headers=headers,
        params={"date_from": "2026-07-01", "date_to": "2026-07-31"},
    )
    assert report.status_code == 200, report.text
    report_groups = {item["currency"]: item for item in report.json()["groups"]}
    assert report_groups["RUB"]["income"] == actual["actual_income"]
    assert report_groups["RUB"]["expense"] == actual["actual_expense"]
    assert report_groups["RUB"]["adjustment"] == actual["adjustment"]
    report_categories = {
        item["category_id"]: item["amount"] for item in report_groups["RUB"]["spending_by_category"]
    }
    assert report_categories[str(parent["id"])] == by_category[str(parent["id"])]["actual"]
    assert report_categories[str(child["id"])] == by_category[str(child["id"])]["actual"]


def test_partial_split_refund_rounding_exactly_matches_reports(client: TestClient) -> None:
    _, headers = _register(client, "Budget split parity")
    account = _account(client, headers)
    first = _category(client, headers, name="Split A")
    second = _category(client, headers, name="Split B")
    original = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        transaction_type="expense",
        amount="100.0000",
        splits=[
            {"category_id": str(first["id"]), "amount": "33.3333"},
            {"category_id": str(second["id"]), "amount": "66.6667"},
        ],
    )
    _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        transaction_type="refund",
        amount="10.0001",
        related_transaction_id=str(original["id"]),
    )
    budget = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        allocations=[
            {"category_id": first["id"], "planned_amount": "50"},
            {"category_id": second["id"], "planned_amount": "70"},
        ],
    )
    assert budget.status_code == 200, budget.text
    payload = budget.json()
    actuals = {item["category_id"]: item["actual"] for item in payload["allocations"]}
    assert actuals[str(first["id"])] == "29.9999"
    assert actuals[str(second["id"])] == "60.0000"
    assert sum((Decimal(value) for value in actuals.values()), start=Decimal("0")) == Decimal(
        payload["actual_expense"]
    )
    assert payload["actual_expense"] == "89.9999"

    report = client.get(
        "/api/v1/reports/financial",
        headers=headers,
        params={"date_from": "2026-07-01", "date_to": "2026-07-31"},
    )
    assert report.status_code == 200, report.text
    report_group = next(item for item in report.json()["groups"] if item["currency"] == "RUB")
    report_actuals = {
        item["category_id"]: item["amount"] for item in report_group["spending_by_category"]
    }
    assert report_group["expense"] == payload["actual_expense"]
    assert report_actuals == actuals


def test_live_predecessor_owns_rollover_policy_and_missing_is_provisional(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Budget rollover")
    account = _account(client, headers)
    category = _category(client, headers)
    allocation = [{"category_id": category["id"], "planned_amount": "100"}]

    june = _budget(
        client,
        headers,
        "2026-06",
        "RUB",
        rollover_policy="positive_only",
        allocations=allocation,
    ).json()
    _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        transaction_type="expense",
        amount="60",
        category_id=str(category["id"]),
        occurred_at="2026-06-10T10:00:00Z",
    )
    july = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        planned_income="10",
        rollover_policy="none",
        allocations=[{"category_id": category["id"], "planned_amount": "55"}],
    ).json()
    assert july["rollover"] == {
        "amount": "40.0000",
        "source_policy": "positive_only",
        "provisional": True,
    }
    assert july["rollover_policy"] == "none"
    assert july["planning_capacity"] == "50.0000"
    assert july["unallocated"] == "-5.0000"

    july_changed = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        planned_income="10",
        rollover_policy="full",
        allocations=[{"category_id": category["id"], "planned_amount": "55"}],
        version=july["version"],
    )
    assert july_changed.status_code == 200, july_changed.text
    assert july_changed.json()["rollover"] == {
        "amount": "40.0000",
        "source_policy": "positive_only",
        "provisional": True,
    }
    assert july_changed.json()["rollover_policy"] == "full"

    june_none = _budget(
        client,
        headers,
        "2026-06",
        "RUB",
        rollover_policy="none",
        allocations=allocation,
        version=int(june["version"]),
    )
    assert june_none.status_code == 200, june_none.text
    assert june_none.json()["remaining"] == "40.0000"
    july_live = client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()
    assert july_live["rollover"]["amount"] == "0.0000"
    assert july_live["rollover"]["source_policy"] == "none"
    assert july_live["rollover_policy"] == "full"
    assert july_live["remaining"] == "55.0000"

    june_full = _budget(
        client,
        headers,
        "2026-06",
        "RUB",
        rollover_policy="full",
        allocations=[{"category_id": category["id"], "planned_amount": "50"}],
        version=june_none.json()["version"],
    )
    assert june_full.status_code == 200, june_full.text
    assert june_full.json()["remaining"] == "-10.0000"
    july_after_predecessor_change = client.get(
        "/api/v1/budgets/2026-07/RUB", headers=headers
    ).json()
    assert july_after_predecessor_change["rollover"] == {
        "amount": "-10.0000",
        "source_policy": "full",
        "provisional": True,
    }
    assert july_after_predecessor_change["rollover_policy"] == "full"

    deleted = client.delete(
        f"/api/v1/budgets/2026-06/RUB?version={june_full.json()['version']}",
        headers={**headers, "X-Idempotency-Key": f"delete-{uuid.uuid4()}"},
    )
    assert deleted.status_code == 200
    without_predecessor = client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()
    assert without_predecessor["rollover"] == {
        "amount": "0.0000",
        "source_policy": "none",
        "provisional": True,
    }
    usd = _budget(
        client,
        headers,
        "2026-07",
        "USD",
        planned_income="5",
        rollover_policy="full",
    ).json()
    assert usd["rollover"] == {
        "amount": "0.0000",
        "source_policy": "none",
        "provisional": True,
    }


def test_frozen_predecessor_rollover_uses_close_snapshot(client: TestClient) -> None:
    cases = (
        ("full", "50", "60", "none", "-10.0000"),
        ("positive_only", "50", "60", "none", "0.0000"),
        ("none", "100", "60", "full", "0.0000"),
    )
    for index, (june_policy, planned, expense, july_policy, expected) in enumerate(cases):
        _, headers = _register(client, f"Budget frozen predecessor {index}")
        account = _account(client, headers)
        category = _category(client, headers)
        _budget(
            client,
            headers,
            "2026-06",
            "RUB",
            rollover_policy=june_policy,
            allocations=[{"category_id": category["id"], "planned_amount": planned}],
        )
        _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            transaction_type="expense",
            amount=expense,
            category_id=str(category["id"]),
            occurred_at="2026-06-10T10:00:00Z",
        )
        june_close = _prepare(client, headers, "2026-06")
        _confirm(client, headers, "2026-06", june_close)

        july = _budget(
            client,
            headers,
            "2026-07",
            "RUB",
            rollover_policy=july_policy,
            allocations=[{"category_id": category["id"], "planned_amount": "50"}],
        )
        assert july.status_code == 200, july.text
        assert july.json()["rollover"] == {
            "amount": expected,
            "source_policy": june_policy,
            "provisional": False,
        }
        assert july.json()["rollover_policy"] == july_policy

    _, empty_headers = _register(client, "Budget frozen missing predecessor")
    empty_close = _prepare(client, empty_headers, "2026-06")
    _confirm(client, empty_headers, "2026-06", empty_close)
    after_empty_close = _budget(
        client,
        empty_headers,
        "2026-07",
        "RUB",
        rollover_policy="full",
    )
    assert after_empty_close.status_code == 200, after_empty_close.text
    assert after_empty_close.json()["rollover"] == {
        "amount": "0.0000",
        "source_policy": "none",
        "provisional": False,
    }


def test_copy_preserves_outgoing_policy_but_not_incoming_policy(client: TestClient) -> None:
    _, headers = _register(client, "Budget copy rollover ownership")
    account = _account(client, headers)
    category = _category(client, headers)
    _budget(
        client,
        headers,
        "2026-05",
        "RUB",
        rollover_policy="full",
        allocations=[{"category_id": category["id"], "planned_amount": "10"}],
    )
    _budget(
        client,
        headers,
        "2026-06",
        "RUB",
        rollover_policy="none",
        allocations=[{"category_id": category["id"], "planned_amount": "100"}],
    )
    _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        transaction_type="expense",
        amount="60",
        category_id=str(category["id"]),
        occurred_at="2026-06-10T10:00:00Z",
    )
    copied = client.post(
        "/api/v1/budgets/2026-07/RUB/copy",
        headers={**headers, "X-Idempotency-Key": f"copy-{uuid.uuid4()}"},
        json={"source_period": "2026-05"},
    )
    assert copied.status_code == 200, copied.text
    assert copied.json()["rollover_policy"] == "full"
    assert copied.json()["rollover"] == {
        "amount": "0.0000",
        "source_policy": "none",
        "provisional": True,
    }


def test_allocation_validation_is_atomic_and_failed_key_is_reusable(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Budget allocation validation")
    active = _category(client, headers, name="Active expense")
    archived = _category(client, headers, name="Archived expense")
    deleted = _category(client, headers, name="Deleted expense")
    income = _category(client, headers, "income", name="Income only")
    _, other_headers = _register(client, "Other allocation workspace")
    foreign = _category(client, other_headers, name="Foreign expense")

    archived_response = client.patch(
        f"/api/v1/categories/{archived['id']}",
        headers=headers,
        json={"version": archived["version"], "is_archived": True},
    )
    assert archived_response.status_code == 200, archived_response.text
    deleted_response = client.delete(
        f"/api/v1/categories/{deleted['id']}?version={deleted['version']}", headers=headers
    )
    assert deleted_response.status_code == 200, deleted_response.text

    initial = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        allocations=[{"category_id": active["id"], "planned_amount": "10", "note": "original"}],
    ).json()
    invalid_allocations = [
        [
            {"category_id": active["id"], "planned_amount": "10"},
            {"category_id": active["id"], "planned_amount": "20"},
        ],
        [{"category_id": foreign["id"], "planned_amount": "10"}],
        [{"category_id": archived["id"], "planned_amount": "10"}],
        [{"category_id": deleted["id"], "planned_amount": "10"}],
        [{"category_id": income["id"], "planned_amount": "10"}],
        [{"category_id": active["id"], "planned_amount": "0"}],
        [{"category_id": active["id"], "planned_amount": "-1"}],
    ]
    for allocations in invalid_allocations:
        response = _budget(
            client,
            headers,
            "2026-07",
            "RUB",
            version=initial["version"],
            allocations=allocations,
        )
        assert response.status_code == 422, response.text
        current = client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()
        assert current["version"] == initial["version"]
        actual_allocations = [
            (item["category_id"], item["planned"], item["note"]) for item in current["allocations"]
        ]
        assert actual_allocations == [(active["id"], "10.0000", "original")]

    reusable_key = f"failed-then-valid-{uuid.uuid4()}"
    failed = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        version=initial["version"],
        allocations=invalid_allocations[0],
        key=reusable_key,
    )
    assert failed.status_code == 422
    successful = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        version=initial["version"],
        allocations=[{"category_id": active["id"], "planned_amount": "11"}],
        key=reusable_key,
    )
    assert successful.status_code == 200, successful.text
    assert successful.json()["allocations"][0]["planned"] == "11.0000"

    deleted_budget = client.delete(
        f"/api/v1/budgets/2026-07/RUB?version={successful.json()['version']}",
        headers={**headers, "X-Idempotency-Key": f"delete-{uuid.uuid4()}"},
    )
    assert deleted_budget.status_code == 200, deleted_budget.text
    duplicate_create = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        allocations=[{"category_id": active["id"], "planned_amount": "12"}],
    )
    assert duplicate_create.status_code == 409
    assert duplicate_create.json()["error"]["code"] == "BUDGET_RESTORE_REQUIRED"


def test_lifecycle_idempotency_revisions_audit_permissions_and_no_google_outbox(
    client: TestClient,
) -> None:
    identity, owner = _register(client, "Budget lifecycle")
    category = _category(client, owner)
    income_category = _category(client, owner, "income")
    create_key = f"budget-create-{uuid.uuid4()}"
    created_response = _budget(
        client,
        owner,
        "2026-07",
        "RUB",
        planned_income="100",
        allocations=[{"category_id": category["id"], "planned_amount": "60"}],
        key=create_key,
    )
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()
    invalid_category = _budget(
        client,
        owner,
        "2026-08",
        "RUB",
        allocations=[{"category_id": income_category["id"], "planned_amount": "1"}],
    )
    assert invalid_category.status_code == 422
    assert invalid_category.json()["error"]["code"] == "BUDGET_CATEGORY_INVALID"

    updated_response = _budget(
        client,
        owner,
        "2026-07",
        "RUB",
        planned_income="150",
        allocations=[{"category_id": category["id"], "planned_amount": "70", "note": "new"}],
        version=created["version"],
    )
    assert updated_response.status_code == 200, updated_response.text
    updated = updated_response.json()
    assert len(updated["allocations"]) == 1
    replay = _budget(
        client,
        owner,
        "2026-07",
        "RUB",
        planned_income="100",
        allocations=[{"category_id": category["id"], "planned_amount": "60"}],
        key=create_key,
    )
    assert replay.status_code == 200
    assert replay.json() == created
    changed_replay = _budget(
        client,
        owner,
        "2026-07",
        "RUB",
        planned_income="101",
        allocations=[{"category_id": category["id"], "planned_amount": "60"}],
        key=create_key,
    )
    assert changed_replay.status_code == 409
    assert changed_replay.json()["error"]["code"] == "BUDGET_IDEMPOTENCY_CONFLICT"

    stale = _budget(
        client,
        owner,
        "2026-07",
        "RUB",
        version=created["version"],
        planned_income="10",
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "BUDGET_VERSION_CONFLICT"

    delete_key = f"delete-{uuid.uuid4()}"
    deleted_response = client.delete(
        f"/api/v1/budgets/2026-07/RUB?version={updated['version']}",
        headers={**owner, "X-Idempotency-Key": delete_key},
    )
    assert deleted_response.status_code == 200, deleted_response.text
    deleted = deleted_response.json()
    assert deleted["id"] == created["id"]
    restore_key = f"restore-{uuid.uuid4()}"
    restored_response = client.post(
        "/api/v1/budgets/2026-07/RUB/restore",
        headers={**owner, "X-Idempotency-Key": restore_key},
        json={"version": deleted["version"]},
    )
    assert restored_response.status_code == 200, restored_response.text
    restored = restored_response.json()
    assert restored["id"] == created["id"]
    assert restored["allocations"][0]["planned"] == "70.0000"
    delete_replay = client.delete(
        f"/api/v1/budgets/2026-07/RUB?version={updated['version']}",
        headers={**owner, "X-Idempotency-Key": delete_key},
    )
    assert delete_replay.status_code == 200
    assert delete_replay.json() == deleted

    copy_key = f"copy-{uuid.uuid4()}"
    copied_response = client.post(
        "/api/v1/budgets/2026-08/RUB/copy",
        headers={**owner, "X-Idempotency-Key": copy_key},
        json={"source_period": "2026-07"},
    )
    assert copied_response.status_code == 200, copied_response.text
    copied = copied_response.json()
    assert copied["planned_income"] == "150.0000"
    assert copied["allocations"][0]["planned"] == "70.0000"
    overwrite_missing = client.post(
        "/api/v1/budgets/2026-08/RUB/copy",
        headers={**owner, "X-Idempotency-Key": f"copy-{uuid.uuid4()}"},
        json={"source_period": "2026-07", "overwrite": True, "version": 999},
    )
    assert overwrite_missing.status_code == 409
    assert overwrite_missing.json()["error"]["code"] == "BUDGET_VERSION_CONFLICT"
    overwritten = client.post(
        "/api/v1/budgets/2026-08/RUB/copy",
        headers={**owner, "X-Idempotency-Key": f"copy-{uuid.uuid4()}"},
        json={
            "source_period": "2026-07",
            "overwrite": True,
            "version": copied["version"],
        },
    )
    assert overwritten.status_code == 200, overwritten.text
    assert overwritten.json()["version"] == copied["version"] + 1
    copy_replay = client.post(
        "/api/v1/budgets/2026-08/RUB/copy",
        headers={**owner, "X-Idempotency-Key": copy_key},
        json={"source_period": "2026-07"},
    )
    assert copy_replay.status_code == 200
    assert copy_replay.json() == copied

    updated_again_response = _budget(
        client,
        owner,
        "2026-07",
        "RUB",
        planned_income="160",
        allocations=[{"category_id": category["id"], "planned_amount": "80"}],
        version=restored["version"],
    )
    assert updated_again_response.status_code == 200, updated_again_response.text
    restore_replay = client.post(
        "/api/v1/budgets/2026-07/RUB/restore",
        headers={**owner, "X-Idempotency-Key": restore_key},
        json={"version": deleted["version"]},
    )
    assert restore_replay.status_code == 200
    assert restore_replay.json() == restored
    create_replay_after_all = _budget(
        client,
        owner,
        "2026-07",
        "RUB",
        planned_income="100",
        allocations=[{"category_id": category["id"], "planned_amount": "60"}],
        key=create_key,
    )
    assert create_replay_after_all.status_code == 200
    assert create_replay_after_all.json() == created

    async def add_members_and_inspect() -> tuple[
        dict[str, str], dict[str, str], tuple[int, int, int]
    ]:
        async with AsyncSessionFactory() as session:
            workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
            result: list[dict[str, str]] = []
            for role in ("viewer", "editor"):
                user = User(
                    email=f"budget-{role}-{uuid.uuid4()}@example.com",
                    normalized_email=f"budget-{role}-{uuid.uuid4()}@example.com",
                    display_name=f"Budget {role}",
                )
                session.add(user)
                await session.flush()
                session.add(WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=role))
                result.append({"X-User-ID": str(user.id), "X-Workspace-ID": str(workspace_id)})
            await session.commit()
            revision_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetPlanRevision)
                    .where(BudgetPlanRevision.workspace_id == workspace_id)
                )
                or 0
            )
            audit_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.workspace_id == workspace_id,
                        AuditLog.entity_type == "budget_period",
                    )
                )
                or 0
            )
            outbox_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncOutbox)
                    .where(
                        SyncOutbox.workspace_id == workspace_id,
                        SyncOutbox.entity_type == "budget_period",
                    )
                )
                or 0
            )
            return result[0], result[1], (revision_count, audit_count, outbox_count)

    viewer, editor, counts = asyncio.run(add_members_and_inspect())
    assert client.get("/api/v1/budgets/2026-07", headers=viewer).status_code == 200
    forbidden = _budget(client, viewer, "2026-09", "RUB")
    assert forbidden.status_code == 403
    assert _budget(client, editor, "2026-09", "RUB").status_code == 200
    _, service_key = _service_key(client, owner, ["reports:generate"])
    service_response = client.get(
        "/api/v1/budgets/2026-07",
        headers={
            "Authorization": f"ServiceKey {service_key}",
            "X-Workspace-ID": identity["workspace"]["id"],
        },
    )
    assert service_response.status_code == 401
    assert service_response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert counts[0] == counts[1] == 7
    assert counts[2] == 0
    history = client.get("/api/v1/budgets/2026-07/RUB/history", headers=viewer)
    assert history.status_code == 200
    assert history.json()["page"]["total"] == 5
    assert all("allocations" in item["snapshot"] for item in history.json()["items"])
    paged_history = client.get(
        "/api/v1/budgets/2026-07/RUB/history?limit=1&offset=1", headers=viewer
    )
    assert paged_history.status_code == 200
    assert len(paged_history.json()["items"]) == 1
    assert paged_history.json()["page"] == {"limit": 1, "offset": 1, "total": 5}
    assert (
        client.get("/api/v1/budgets/2026-07/RUB/history?limit=101", headers=viewer).status_code
        == 422
    )
    audit = client.get(
        f"/api/v1/audit?entity_type=budget_period&entity_id={created['id']}",
        headers=owner,
    )
    assert audit.status_code == 200
    assert {item["action"] for item in audit.json()["items"]} == {
        "create",
        "update",
        "delete",
        "restore",
    }
    assert all("allocations" in item["after_data"] for item in audit.json()["items"])
    copy_audit = client.get(
        f"/api/v1/audit?entity_type=budget_period&entity_id={copied['id']}",
        headers=owner,
    )
    assert copy_audit.status_code == 200
    copy_items = [
        item
        for item in copy_audit.json()["items"]
        if item["after_data"].get("budget_operation") == "copy"
    ]
    assert len(copy_items) == 2
    assert {item["action"] for item in copy_items} == {"create", "update"}
    assert all(item["after_data"]["copy_source_period"] == "2026-07" for item in copy_items)
    assert all(item["after_data"]["copy_source_budget_id"] == created["id"] for item in copy_items)

    async def revision_evidence() -> tuple[dict[str, object], list[str]]:
        async with AsyncSessionFactory() as session:
            revision = await session.scalar(
                select(BudgetPlanRevision).where(
                    BudgetPlanRevision.workspace_id == uuid.UUID(str(identity["workspace"]["id"])),
                    BudgetPlanRevision.idempotency_key == create_key,
                )
            )
            assert revision is not None
            copy_revision_actions = list(
                (
                    await session.scalars(
                        select(BudgetPlanRevision.action)
                        .where(
                            BudgetPlanRevision.workspace_id
                            == uuid.UUID(str(identity["workspace"]["id"])),
                            BudgetPlanRevision.budget_period_id == uuid.UUID(str(copied["id"])),
                        )
                        .order_by(BudgetPlanRevision.revision_number)
                    )
                ).all()
            )
            return dict(revision.response_snapshot), copy_revision_actions

    original_response, copy_revision_actions = asyncio.run(revision_evidence())
    assert original_response == created
    assert copy_revision_actions == ["copy", "copy"]
    _, other_workspace = _register(client, "Other budget workspace")
    assert client.get("/api/v1/budgets/2026-07", headers=other_workspace).json()["groups"] == []
    assert client.get("/api/v1/budgets/2026-07/RUB", headers=other_workspace).status_code == 404


def test_idempotency_key_binds_action_identity_and_full_semantic_request(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Budget idempotency identity")
    category = _category(client, headers)
    key = f"identity-{uuid.uuid4()}"
    created = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        planned_income="100",
        allocations=[{"category_id": category["id"], "planned_amount": "40"}],
        key=key,
    ).json()

    conflicts = [
        _budget(
            client,
            headers,
            "2026-08",
            "RUB",
            planned_income="100",
            allocations=[{"category_id": category["id"], "planned_amount": "40"}],
            key=key,
        ),
        _budget(
            client,
            headers,
            "2026-07",
            "USD",
            planned_income="100",
            allocations=[{"category_id": category["id"], "planned_amount": "40"}],
            key=key,
        ),
        _budget(
            client,
            headers,
            "2026-07",
            "RUB",
            planned_income="101",
            allocations=[{"category_id": category["id"], "planned_amount": "40"}],
            key=key,
        ),
        _budget(
            client,
            headers,
            "2026-07",
            "RUB",
            planned_income="100",
            allocations=[{"category_id": category["id"], "planned_amount": "40"}],
            version=created["version"],
            key=key,
        ),
        client.delete(
            f"/api/v1/budgets/2026-07/RUB?version={created['version']}",
            headers={**headers, "X-Idempotency-Key": key},
        ),
    ]
    for conflict in conflicts:
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "BUDGET_IDEMPOTENCY_CONFLICT"

    copy_key = f"copy-identity-{uuid.uuid4()}"
    copied = client.post(
        "/api/v1/budgets/2026-08/RUB/copy",
        headers={**headers, "X-Idempotency-Key": copy_key},
        json={"source_period": "2026-07"},
    )
    assert copied.status_code == 200, copied.text
    overwrite_conflict = client.post(
        "/api/v1/budgets/2026-08/RUB/copy",
        headers={**headers, "X-Idempotency-Key": copy_key},
        json={
            "source_period": "2026-07",
            "overwrite": True,
            "version": copied.json()["version"],
        },
    )
    assert overwrite_conflict.status_code == 409
    assert overwrite_conflict.json()["error"]["code"] == "BUDGET_IDEMPOTENCY_CONFLICT"


def test_restore_and_copy_fail_explicitly_for_archived_categories(client: TestClient) -> None:
    _, headers = _register(client, "Budget category conflicts")
    category = _category(client, headers)
    source = _budget(
        client,
        headers,
        "2026-06",
        "RUB",
        allocations=[{"category_id": category["id"], "planned_amount": "10"}],
    ).json()
    deleted = client.delete(
        f"/api/v1/budgets/2026-06/RUB?version={source['version']}",
        headers={**headers, "X-Idempotency-Key": f"delete-{uuid.uuid4()}"},
    ).json()
    archived = client.patch(
        f"/api/v1/categories/{category['id']}",
        headers=headers,
        json={"version": category["version"], "is_archived": True},
    )
    assert archived.status_code == 200, archived.text
    restore = client.post(
        "/api/v1/budgets/2026-06/RUB/restore",
        headers={**headers, "X-Idempotency-Key": f"restore-{uuid.uuid4()}"},
        json={"version": deleted["version"]},
    )
    assert restore.status_code == 409
    assert restore.json()["error"]["code"] == "BUDGET_RESTORE_CATEGORY_CONFLICT"

    active_source = _budget(
        client,
        headers,
        "2026-07",
        "USD",
        allocations=[],
    ).json()
    assert active_source["currency"] == "USD"

    async def attach_archived_allocation() -> None:
        async with AsyncSessionFactory() as session:
            budget = await session.scalar(
                select(BudgetPeriod).where(BudgetPeriod.id == uuid.UUID(str(active_source["id"])))
            )
            assert budget is not None
            session.add(
                BudgetAllocation(
                    budget_period_id=budget.id,
                    category_id=uuid.UUID(str(category["id"])),
                    planned_amount=Decimal("10"),
                )
            )
            await session.commit()

    asyncio.run(attach_archived_allocation())
    copy = client.post(
        "/api/v1/budgets/2026-08/USD/copy",
        headers={**headers, "X-Idempotency-Key": f"copy-{uuid.uuid4()}"},
        json={"source_period": "2026-07"},
    )
    assert copy.status_code == 409
    assert copy.json()["error"]["code"] == "BUDGET_COPY_CATEGORY_CONFLICT"


def test_month_close_planning_snapshot_freeze_reopen_and_first_close_edge(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Budget close")
    category = _category(client, headers)
    june = _budget(
        client,
        headers,
        "2026-06",
        "RUB",
        rollover_policy="full",
        allocations=[{"category_id": category["id"], "planned_amount": "100"}],
    ).json()
    july = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        planned_income="20",
        rollover_policy="full",
        allocations=[{"category_id": category["id"], "planned_amount": "50"}],
    ).json()
    prepared = _prepare(client, headers, "2026-07")
    planning = prepared["summary"]["planning_budget"]
    assert planning["schema_version"] == 1
    assert len(planning["groups"]) == 1
    assert len(prepared["summary"]["budget_plan_fingerprint"]) == 64
    first_budget_fingerprint = prepared["summary"]["budget_plan_fingerprint"]
    first_prepare_token = prepared["prepare_token"]
    financial_fingerprint = prepared["prepared_fingerprint"]

    edited = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        planned_income="25",
        rollover_policy="full",
        allocations=[{"category_id": category["id"], "planned_amount": "50"}],
        version=july["version"],
    )
    assert edited.status_code == 200
    stale = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": f"close-{uuid.uuid4()}"},
        json={
            "version": prepared["version"],
            "confirm": True,
            "prepare_token": prepared["prepare_token"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "MONTH_CLOSE_PREVIEW_STALE"

    prepared = _prepare(client, headers, "2026-07")
    assert prepared["prepared_fingerprint"] == financial_fingerprint
    assert prepared["summary"]["budget_plan_fingerprint"] != first_budget_fingerprint
    assert prepared["prepare_token"] != first_prepare_token
    closed = _confirm(client, headers, "2026-07", prepared)
    frozen = client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()
    assert frozen["frozen"] is True
    assert frozen["projection_source"] == "month_close_revision"
    frozen_rollover = frozen["rollover"]
    original_category_name = frozen["allocations"][0]["category_name"]
    renamed = client.patch(
        f"/api/v1/categories/{category['id']}",
        headers=headers,
        json={"version": category["version"], "name": "Renamed after close"},
    )
    assert renamed.status_code == 200, renamed.text
    frozen_after_rename = client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()
    assert frozen_after_rename["allocations"][0]["category_name"] == original_category_name
    rejected = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        planned_income="30",
        version=edited.json()["version"],
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "BUDGET_PERIOD_FROZEN"

    june_update = _budget(
        client,
        headers,
        "2026-06",
        "RUB",
        rollover_policy="full",
        allocations=[{"category_id": category["id"], "planned_amount": "10"}],
        version=june["version"],
    )
    assert june_update.status_code == 200, june_update.text
    assert (
        client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()["rollover"]
        == frozen_rollover
    )

    reopened_response = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**headers, "X-Idempotency-Key": f"reopen-{uuid.uuid4()}"},
        json={"version": closed["version"], "reason": "Budget correction after close"},
    )
    assert reopened_response.status_code == 200, reopened_response.text
    assert reopened_response.json()["current_revision_id"] == closed["current_revision_id"]
    live = client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()
    assert live["frozen"] is False
    assert live["rollover"]["amount"] == "10.0000"
    assert live["rollover"]["provisional"] is True
    assert live["allocations"][0]["category_name"] == "Renamed after close"
    second_edit = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        planned_income="35",
        rollover_policy="full",
        allocations=[{"category_id": category["id"], "planned_amount": "50"}],
        version=live["version"],
    )
    assert second_edit.status_code == 200
    second_prepared = _prepare(client, headers, "2026-07")
    second_closed = _confirm(client, headers, "2026-07", second_prepared)
    assert second_closed["current_revision_id"] != closed["current_revision_id"]

    history = client.get("/api/v1/month-close/2026/7/history", headers=headers).json()
    assert history["page"]["total"] == 2
    snapshots = [item["snapshot_summary"]["planning_budget"] for item in history["items"]]
    incomes = {snapshot["groups"][0]["planned_income"] for snapshot in snapshots}
    assert incomes == {"25.0000", "35.0000"}
    category_names = {
        snapshot["groups"][0]["allocations"][0]["category_name"] for snapshot in snapshots
    }
    assert category_names == {original_category_name, "Renamed after close"}

    async def revision_fingerprints() -> tuple[set[str], str]:
        async with AsyncSessionFactory() as session:
            workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
            rows = list(
                (
                    await session.scalars(
                        select(MonthCloseRevision).where(
                            MonthCloseRevision.workspace_id == workspace_id
                        )
                    )
                ).all()
            )
            return {
                row.financial_fingerprint for row in rows if row.financial_fingerprint is not None
            }, str(financial_fingerprint)

    fingerprints, expected = asyncio.run(revision_fingerprints())
    assert fingerprints == {expected}


def test_empty_budget_month_close_snapshot_is_explicit(client: TestClient) -> None:
    _, headers = _register(client, "Empty budget close")
    prepared = _prepare(client, headers, "2026-07")
    assert prepared["summary"]["planning_budget"] == {
        "schema_version": 1,
        "period": "2026-07",
        "timezone": "Asia/Yekaterinburg",
        "groups": [],
    }
    assert len(prepared["summary"]["budget_plan_fingerprint"]) == 64


def test_concurrent_first_create_has_one_deterministic_loser(client: TestClient) -> None:
    identity, _ = _register(client, "Budget concurrent first create")
    workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
    user_id = uuid.UUID(str(identity["user"]["id"]))

    async def create(key: str, planned_income: str) -> object:
        async with AsyncSessionFactory() as session:
            user = await session.get(User, user_id)
            workspace = await session.get(Workspace, workspace_id)
            assert user is not None and workspace is not None
            context = RequestContext(user=user, workspace=workspace, role="owner", request_id=key)
            try:
                return await budget_service.upsert(
                    session,
                    context,
                    date(2026, 7, 1),
                    "RUB",
                    BudgetUpsertRequest.model_validate(
                        {"planned_income": planned_income, "allocations": []}
                    ),
                    key,
                )
            except ApiError as error:
                await session.rollback()
                return error

    async def run() -> tuple[object, object]:
        return await asyncio.gather(
            create(f"first-a-{uuid.uuid4()}", "10"),
            create(f"first-b-{uuid.uuid4()}", "20"),
        )

    results = asyncio.run(run())
    assert sum(not isinstance(item, ApiError) for item in results) == 1
    errors = [item for item in results if isinstance(item, ApiError)]
    assert len(errors) == 1
    assert errors[0].code == "BUDGET_VERSION_CONFLICT"

    async def counts() -> tuple[int, int]:
        async with AsyncSessionFactory() as session:
            periods = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetPeriod)
                    .where(
                        BudgetPeriod.workspace_id == workspace_id,
                        BudgetPeriod.period_month == date(2026, 7, 1),
                        BudgetPeriod.currency == "RUB",
                    )
                )
                or 0
            )
            revisions = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetPlanRevision)
                    .where(BudgetPlanRevision.workspace_id == workspace_id)
                )
                or 0
            )
            return periods, revisions

    assert asyncio.run(counts()) == (1, 1)


def test_concurrent_same_version_and_same_idempotency_are_serialized(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Budget concurrency")
    category = _category(client, headers)
    created = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        allocations=[{"category_id": category["id"], "planned_amount": "10"}],
    ).json()
    workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
    user_id = uuid.UUID(str(identity["user"]["id"]))

    async def update(key: str, amount: str) -> object:
        async with AsyncSessionFactory() as session:
            user = await session.get(User, user_id)
            workspace = await session.get(Workspace, workspace_id)
            assert user is not None and workspace is not None
            context = RequestContext(user=user, workspace=workspace, role="owner", request_id=key)
            try:
                return await budget_service.upsert(
                    session,
                    context,
                    date(2026, 7, 1),
                    "RUB",
                    BudgetUpsertRequest.model_validate(
                        {
                            "version": created["version"],
                            "planned_income": amount,
                            "allocations": [
                                {"category_id": category["id"], "planned_amount": "10"}
                            ],
                        }
                    ),
                    key,
                )
            except ApiError as error:
                await session.rollback()
                return error

    async def run_different() -> tuple[object, object]:
        return await asyncio.gather(
            update(f"different-a-{uuid.uuid4()}", "20"),
            update(f"different-b-{uuid.uuid4()}", "30"),
        )

    different = asyncio.run(run_different())
    assert sum(not isinstance(item, ApiError) for item in different) == 1
    errors = [item for item in different if isinstance(item, ApiError)]
    assert len(errors) == 1 and errors[0].code == "BUDGET_VERSION_CONFLICT"

    current = client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()
    same_key = f"same-{uuid.uuid4()}"

    async def replay() -> BudgetGroupResponse:
        async with AsyncSessionFactory() as session:
            user = await session.get(User, user_id)
            workspace = await session.get(Workspace, workspace_id)
            assert user is not None and workspace is not None
            context = RequestContext(
                user=user,
                workspace=workspace,
                role="owner",
                request_id=same_key,
            )
            return await budget_service.upsert(
                session,
                context,
                date(2026, 7, 1),
                "RUB",
                BudgetUpsertRequest.model_validate(
                    {
                        "version": current["version"],
                        "planned_income": "40",
                        "allocations": [{"category_id": category["id"], "planned_amount": "10"}],
                    }
                ),
                same_key,
            )

    async def run_same() -> tuple[BudgetGroupResponse, BudgetGroupResponse]:
        return await asyncio.gather(replay(), replay())

    same = asyncio.run(run_same())
    assert same[0].model_dump(mode="json") == same[1].model_dump(mode="json")

    async def revision_count() -> int:
        async with AsyncSessionFactory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetPlanRevision)
                    .where(
                        BudgetPlanRevision.workspace_id == workspace_id,
                        BudgetPlanRevision.idempotency_key == same_key,
                    )
                )
                or 0
            )

    assert asyncio.run(revision_count()) == 1


def test_budget_mutation_and_month_close_confirm_lock_order_races(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, headers = _register(client, "Budget close races")
    category = _category(client, headers)
    initial = _budget(
        client,
        headers,
        "2026-07",
        "RUB",
        allocations=[{"category_id": category["id"], "planned_amount": "10"}],
    ).json()
    workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
    user_id = uuid.UUID(str(identity["user"]["id"]))

    async def context(session: AsyncSession, request_id: str) -> RequestContext:
        user = await session.get(User, user_id)
        workspace = await session.get(Workspace, workspace_id)
        assert user is not None and workspace is not None
        return RequestContext(user=user, workspace=workspace, role="owner", request_id=request_id)

    prepared = _prepare(client, headers, "2026-07")
    mutation_holds_control = asyncio.Event()
    release_mutation = asyncio.Event()
    original_record: Any = budget_service._record_success

    async def paused_record(*args: Any, **kwargs: Any) -> None:
        await original_record(*args, **kwargs)
        mutation_holds_control.set()
        await release_mutation.wait()

    monkeypatch.setattr(budget_service, "_record_success", paused_record)

    async def mutate_first() -> object:
        async with AsyncSessionFactory() as session:
            ctx = await context(session, "mutation-first")
            return await budget_service.upsert(
                session,
                ctx,
                date(2026, 7, 1),
                "RUB",
                BudgetUpsertRequest.model_validate(
                    {
                        "version": initial["version"],
                        "planned_income": "1",
                        "allocations": [{"category_id": category["id"], "planned_amount": "10"}],
                    }
                ),
                f"race-mutate-{uuid.uuid4()}",
            )

    async def confirm_after() -> object:
        async with AsyncSessionFactory() as session:
            ctx = await context(session, "confirm-after")
            try:
                return await month_close_service.confirm(
                    session,
                    ctx,
                    date(2026, 7, 1),
                    version=int(prepared["version"]),
                    explicit=True,
                    prepare_token=str(prepared["prepare_token"]),
                    idempotency_key=f"race-confirm-{uuid.uuid4()}",
                )
            except ApiError as error:
                await session.rollback()
                return error

    async def run_mutation_first() -> tuple[object, object]:
        mutation_task = asyncio.create_task(mutate_first())
        await asyncio.wait_for(mutation_holds_control.wait(), timeout=5)
        confirm_task = asyncio.create_task(confirm_after())
        await asyncio.sleep(0.1)
        assert not confirm_task.done()
        release_mutation.set()
        return await mutation_task, await confirm_task

    _, stale = asyncio.run(run_mutation_first())
    assert isinstance(stale, ApiError) and stale.code == "MONTH_CLOSE_PREVIEW_STALE"

    monkeypatch.setattr(budget_service, "_record_success", original_record)
    current = client.get("/api/v1/budgets/2026-07/RUB", headers=headers).json()
    prepared = _prepare(client, headers, "2026-07")
    confirm_holds_control = asyncio.Event()
    release_confirm = asyncio.Event()
    original_collect: Any = month_close_service.collect_preview

    async def paused_collect(*args: Any, **kwargs: Any) -> Any:
        confirm_holds_control.set()
        await release_confirm.wait()
        return await original_collect(*args, **kwargs)

    monkeypatch.setattr(month_close_service, "collect_preview", paused_collect)

    async def confirm_first() -> object:
        async with AsyncSessionFactory() as session:
            ctx = await context(session, "confirm-first")
            return await month_close_service.confirm(
                session,
                ctx,
                date(2026, 7, 1),
                version=int(prepared["version"]),
                explicit=True,
                prepare_token=str(prepared["prepare_token"]),
                idempotency_key=f"race-confirm-{uuid.uuid4()}",
            )

    async def mutate_after() -> object:
        async with AsyncSessionFactory() as session:
            ctx = await context(session, "mutation-after")
            try:
                return await budget_service.upsert(
                    session,
                    ctx,
                    date(2026, 7, 1),
                    "RUB",
                    BudgetUpsertRequest.model_validate(
                        {
                            "version": current["version"],
                            "planned_income": "2",
                            "allocations": [
                                {"category_id": category["id"], "planned_amount": "10"}
                            ],
                        }
                    ),
                    f"race-mutate-{uuid.uuid4()}",
                )
            except ApiError as error:
                await session.rollback()
                return error

    async def run_confirm_first() -> tuple[object, object]:
        confirm_task = asyncio.create_task(confirm_first())
        await asyncio.wait_for(confirm_holds_control.wait(), timeout=5)
        mutation_task = asyncio.create_task(mutate_after())
        await asyncio.sleep(0.1)
        assert not mutation_task.done()
        release_confirm.set()
        return await confirm_task, await mutation_task

    _, frozen = asyncio.run(run_confirm_first())
    assert isinstance(frozen, ApiError) and frozen.code == "BUDGET_PERIOD_FROZEN"
