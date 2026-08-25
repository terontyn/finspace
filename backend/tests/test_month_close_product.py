import asyncio
import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.automations import MonthCloseControl, MonthCloseRevision, MonthClosure
from app.db.models.google_sync import GoogleSheetBinding, SyncOutbox
from app.db.models.imports import ImportBatch
from app.db.session import AsyncSessionFactory
from app.services import month_close as month_close_service
from tests.test_automations import _register
from tests.test_month_close_invariants import (
    _account,
    _category,
    _confirm,
    _force_missing_backup,
    _prepare,
    _role_headers,
    _transaction,
)


@pytest.fixture(autouse=True)
def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _assert_issue_contract(issue: dict[str, object], severity: str) -> None:
    assert issue["severity"] == severity
    assert isinstance(issue["code"], str)
    assert isinstance(issue["scope"], str)
    assert isinstance(issue["count"], int)
    assert isinstance(issue["message"], str)
    assert isinstance(issue["details"], dict)


def test_issue_contract_staging_outbox_and_backup_policy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_missing_backup(monkeypatch)
    identity, headers = _register(client, "Month Close Product Issues")
    account = _account(client, headers, opening_balance="25")
    category = _category(client, headers, "income")
    assert (
        _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            category_id=str(category["id"]),
        ).status_code
        == 201
    )

    async def seed_operational_warnings() -> None:
        async with AsyncSessionFactory() as session:
            workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
            user_id = uuid.UUID(str(identity["user"]["id"]))
            binding = GoogleSheetBinding(
                workspace_id=workspace_id,
                provider="apps_script_bridge",
                spreadsheet_name="Month Close Test",
                template_version=1,
                status="active",
                sync_enabled=True,
                sync_mode="bidirectional",
                apps_script_enabled=True,
                binding_secret_hash="a" * 64,
                created_by=user_id,
            )
            session.add(binding)
            await session.flush()
            session.add(
                SyncOutbox(
                    workspace_id=workspace_id,
                    binding_id=binding.id,
                    entity_type="transaction",
                    entity_id=uuid.UUID(str(account["id"])),
                    operation="upsert",
                    entity_version=1,
                    payload={},
                    idempotency_key=f"failed-{uuid.uuid4()}",
                    status="failed",
                    attempt_count=1,
                    available_at=datetime.now(UTC),
                    last_error_code="TEST_DELIVERY_FAILURE",
                )
            )
            session.add(
                ImportBatch(
                    workspace_id=workspace_id,
                    created_by=user_id,
                    filename="staged.csv",
                    stored_filename=f"{uuid.uuid4()}.csv",
                    file_type="csv",
                    file_size=1,
                    file_sha256="b" * 64,
                    status="uploaded",
                )
            )
            await session.commit()

    asyncio.run(seed_operational_warnings())
    prepared = _prepare(client, headers, 2026, 7)
    assert prepared.status_code == 200, prepared.text
    payload = prepared.json()
    warnings = {item["code"]: item for item in payload["warning_issues"]}
    infos = {item["code"]: item for item in payload["info_issues"]}
    assert "FAILED_SYNC_OUTBOX" in warnings
    assert "FAILED_SYNC_OUTBOX" not in {item["code"] for item in payload["blocking_issues"]}
    assert "STAGED_IMPORTS" in infos
    assert "BACKUP_MISSING" in warnings
    for item in payload["blocking_issues"]:
        _assert_issue_contract(item, "blocker")
    for item in payload["warning_issues"]:
        _assert_issue_contract(item, "warning")
    for item in payload["info_issues"]:
        _assert_issue_contract(item, "info")

    async def require_healthy_backup() -> None:
        async with AsyncSessionFactory() as session:
            control = await session.get(
                MonthCloseControl, uuid.UUID(str(identity["workspace"]["id"]))
            )
            assert control is not None
            control.backup_policy = "require_healthy"
            control.version += 1
            await session.commit()

    asyncio.run(require_healthy_backup())
    blocked = _prepare(client, headers, 2026, 7).json()
    backup_issue = next(
        item for item in blocked["blocking_issues"] if item["code"] == "BACKUP_MISSING"
    )
    _assert_issue_contract(backup_issue, "blocker")


def test_reconciliation_coverage_later_statement_and_zero_activity_semantics(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Month Close Product Reconciliation")
    _account(client, headers, name="Unused zero account", opening_balance="0")
    account = _account(client, headers, name="Material account", opening_balance="100")
    category = _category(client, headers, "income")
    assert (
        _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            category_id=str(category["id"]),
            amount="10",
        ).status_code
        == 201
    )
    first = _prepare(client, headers, 2026, 7).json()
    assert len(first["summary"]["reconciliation_coverage"]) == 1
    coverage = first["summary"]["reconciliation_coverage"][0]
    assert coverage["account_name"] == "Material account"
    assert coverage["state"] == "not_reconciled"
    assert any(item["code"] == "ACCOUNT_NOT_RECONCILED" for item in first["warning_issues"])

    preview = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/preview",
        headers=headers,
        json={
            "statement_date": "2026-08-02",
            "statement_balance": "110.0000",
            "currency": "RUB",
            "account_version": account["version"],
        },
    )
    assert preview.status_code == 200, preview.text
    confirmed = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json={
            "statement_date": preview.json()["statement_date"],
            "statement_balance": preview.json()["statement_balance"],
            "currency": preview.json()["currency"],
            "account_version": account["version"],
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": f"coverage-{uuid.uuid4()}",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    second = _prepare(client, headers, 2026, 7).json()
    coverage = second["summary"]["reconciliation_coverage"][0]
    assert coverage["state"] == "reconciled"
    assert coverage["latest_statement_date"] == "2026-08-02"
    assert not any(item["code"] == "ACCOUNT_NOT_RECONCILED" for item in second["warning_issues"])


def test_month_close_list_is_read_only_for_workspace_without_control(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Month Close Read Only")
    workspace_id = uuid.UUID(str(identity["workspace"]["id"]))

    async def counts() -> tuple[int, int, int, int]:
        async with AsyncSessionFactory() as session:
            return (
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MonthCloseControl)
                        .where(MonthCloseControl.workspace_id == workspace_id)
                    )
                    or 0
                ),
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MonthClosure)
                        .where(MonthClosure.workspace_id == workspace_id)
                    )
                    or 0
                ),
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditLog)
                        .where(AuditLog.workspace_id == workspace_id)
                    )
                    or 0
                ),
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(SyncOutbox)
                        .where(SyncOutbox.workspace_id == workspace_id)
                    )
                    or 0
                ),
            )

    before = asyncio.run(counts())
    response = client.get("/api/v1/month-close?limit=120", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["periods"]
    assert asyncio.run(counts()) == before


def test_history_as_closed_isolation_reclose_and_legacy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, owner_headers = _register(client, "Month Close Product History")
    viewer_headers = _role_headers(identity, "viewer")
    other_identity, other_headers = _register(client, "Other Month Close Workspace")
    account = _account(client, owner_headers, name="Historical account", opening_balance="100")
    usd = _account(
        client,
        owner_headers,
        name="Historical USD",
        currency="USD",
        opening_balance="50",
    )
    category = _category(client, owner_headers, "expense", name="Historical category")
    replacement_category = _category(
        client,
        owner_headers,
        "expense",
        name="Replacement category",
    )
    rub_transaction_response = _transaction(
        client,
        owner_headers,
        account_id=str(account["id"]),
        category_id=str(category["id"]),
        transaction_type="expense",
        amount="20",
    )
    assert rub_transaction_response.status_code == 201
    rub_transaction = rub_transaction_response.json()
    assert (
        _transaction(
            client,
            owner_headers,
            account_id=str(usd["id"]),
            category_id=str(category["id"]),
            transaction_type="expense",
            amount="5",
            currency="USD",
        ).status_code
        == 201
    )
    first_preview = _prepare(client, owner_headers, 2026, 7).json()
    first = _confirm(client, owner_headers, first_preview, 2026, 7)
    assert first.status_code == 200, first.text

    history = client.get("/api/v1/month-close/2026/7/history", headers=viewer_headers)
    assert history.status_code == 200, history.text
    assert history.json()["items"][0]["revision_number"] == 1
    assert history.json()["items"][0]["confirmed_by"]["display_name"]
    assert history.json()["closure"]["capabilities"] == {
        "can_prepare": False,
        "can_confirm": False,
        "can_reopen": False,
        "can_view_history": True,
    }
    assert (
        client.get("/api/v1/month-close/2026/7/history", headers=other_headers).status_code == 404
    )

    report_url = "/api/v1/month-close/2026/7/history/1/report"
    original_report = client.get(report_url, headers=viewer_headers)
    assert original_report.status_code == 200, original_report.text
    original = original_report.json()
    assert original["mode"] == "as_closed"
    assert {item["currency"] for item in original["currencies"]} == {"RUB", "USD"}
    assert any(item["name"] == "Historical account" for item in original["account_balances"])
    assert any(
        category_item["name"] == "Historical category"
        for group in original["category_aggregates"]
        for category_item in group["categories"]
    )

    confirmed = first.json()
    reopened = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**owner_headers, "X-Idempotency-Key": f"reopen-{uuid.uuid4()}"},
        json={"version": confirmed["version"], "reason": "Approved correction"},
    )
    assert reopened.status_code == 200, reopened.text
    updated_transaction = client.patch(
        f"/api/v1/transactions/{rub_transaction['id']}",
        headers=owner_headers,
        json={
            "version": rub_transaction["version"],
            "amount": "27.0000",
            "category_id": replacement_category["id"],
        },
    )
    assert updated_transaction.status_code == 200, updated_transaction.text
    renamed_account = client.patch(
        f"/api/v1/accounts/{account['id']}",
        headers=owner_headers,
        json={"version": account["version"], "name": "Current account name"},
    )
    renamed_category = client.patch(
        f"/api/v1/categories/{category['id']}",
        headers=owner_headers,
        json={"version": category["version"], "name": "Current category name"},
    )
    assert renamed_account.status_code == renamed_category.status_code == 200
    second_preview = _prepare(client, owner_headers, 2026, 7).json()
    second = _confirm(client, owner_headers, second_preview, 2026, 7)
    assert second.status_code == 200, second.text

    second_report = client.get(
        "/api/v1/month-close/2026/7/history/2/report", headers=viewer_headers
    )
    assert second_report.status_code == 200, second_report.text
    second_snapshot = second_report.json()
    assert second_snapshot != original
    assert second_snapshot["financial_fingerprint"] != original["financial_fingerprint"]
    assert (
        next(item for item in second_snapshot["currencies"] if item["currency"] == "RUB")["expense"]
        == "27.0000"
    )
    assert any(
        item["name"] == "Current account name" for item in second_snapshot["account_balances"]
    )
    assert any(
        category_item["name"] == "Replacement category"
        for group in second_snapshot["category_aggregates"]
        for category_item in group["categories"]
    )

    history = client.get("/api/v1/month-close/2026/7/history", headers=viewer_headers).json()
    assert [item["revision_number"] for item in history["items"]] == [2, 1]
    oldest_page = client.get(
        "/api/v1/month-close/2026/7/history?order=oldest&limit=1&offset=0",
        headers=viewer_headers,
    )
    assert oldest_page.status_code == 200, oldest_page.text
    assert oldest_page.json()["page"] == {"limit": 1, "offset": 0, "total": 2}
    assert oldest_page.json()["items"][0]["revision_number"] == 1
    detail = client.get("/api/v1/month-close/2026/7/history/1", headers=viewer_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["revision_number"] == 1
    revision_one = next(item for item in history["items"] if item["revision_number"] == 1)
    assert revision_one["reopened"]["reason"] == "Approved correction"
    assert client.get(report_url, headers=viewer_headers).json() == original
    comparison = client.get(
        "/api/v1/month-close/2026/7/history/1/comparison", headers=viewer_headers
    )
    assert comparison.status_code == 200, comparison.text
    assert any(item["changed"] for item in comparison.json()["differences"]["currencies"])

    second_reopen = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**owner_headers, "X-Idempotency-Key": f"reopen-{uuid.uuid4()}"},
        json={"version": second.json()["version"], "reason": "Second approved correction"},
    )
    assert second_reopen.status_code == 200, second_reopen.text
    third_preview = _prepare(client, owner_headers, 2026, 7).json()
    third = _confirm(client, owner_headers, third_preview, 2026, 7)
    assert third.status_code == 200, third.text
    complete_history = client.get(
        "/api/v1/month-close/2026/7/history", headers=viewer_headers
    ).json()["items"]
    assert [item["revision_number"] for item in complete_history] == [3, 2, 1]
    by_revision = {item["revision_number"]: item for item in complete_history}
    assert by_revision[1]["reopened"]["reason"] == "Approved correction"
    assert by_revision[2]["reopened"]["reason"] == "Second approved correction"
    assert by_revision[3]["reopened"] is None

    async def fail_live_helper(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("as-closed report accessed a live financial helper")

    original_financial_report = month_close_service.financial_report
    original_calculate_balances = month_close_service.calculate_balances
    monkeypatch.setattr(month_close_service, "financial_report", fail_live_helper)
    monkeypatch.setattr(month_close_service, "calculate_balances", fail_live_helper)
    assert client.get(report_url, headers=viewer_headers).json() == original
    monkeypatch.setattr(month_close_service, "financial_report", original_financial_report)
    monkeypatch.setattr(month_close_service, "calculate_balances", original_calculate_balances)

    async def seed_legacy() -> None:
        async with AsyncSessionFactory() as session:
            workspace_id = uuid.UUID(str(other_identity["workspace"]["id"]))
            user_id = uuid.UUID(str(other_identity["user"]["id"]))
            now = datetime.now(UTC)
            closure = MonthClosure(
                workspace_id=workspace_id,
                period_month=date(2026, 6, 1),
                status="confirmed",
                confirmed_by=user_id,
                confirmed_at=now,
                summary={"legacy_unverified": True},
                blocking_issues=[],
                warning_issues=[],
                version=1,
            )
            session.add(closure)
            await session.flush()
            revision = MonthCloseRevision(
                workspace_id=workspace_id,
                closure_id=closure.id,
                revision_number=1,
                period_month=date(2026, 6, 1),
                period_start_at=datetime(2026, 6, 1, tzinfo=UTC),
                period_end_at=datetime(2026, 7, 1, tzinfo=UTC),
                snapshot={"legacy_unverified": True},
                financial_fingerprint=None,
                legacy_unverified=True,
                confirmed_by=user_id,
                confirmed_at=now,
                source="migration",
                idempotency_key=f"legacy-{uuid.uuid4()}",
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            closure.current_revision_id = revision.id
            await session.commit()

    asyncio.run(seed_legacy())
    legacy = client.get("/api/v1/month-close/2026/6/history/1/report", headers=other_headers)
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["legacy_unverified"] is True
    assert legacy.json()["financial_fingerprint"] is None
    assert legacy.json()["currencies"] is None
    assert legacy.json()["transaction_count"] is None
    assert set(legacy.json()["unavailable_sections"]) == {
        "account_balances",
        "category_aggregates",
        "currencies",
        "issue_summary",
        "reconciliation_coverage",
        "transaction_count",
    }
    legacy_detail = client.get("/api/v1/month-close/2026/6/history/1", headers=other_headers)
    assert legacy_detail.status_code == 200, legacy_detail.text
    assert legacy_detail.json()["legacy_unverified"] is True
    assert legacy_detail.json()["financial_fingerprint"] is None
    legacy_comparison = client.get(
        "/api/v1/month-close/2026/6/history/1/comparison", headers=other_headers
    )
    assert legacy_comparison.status_code == 200, legacy_comparison.text
    assert legacy_comparison.json()["differences"] == {
        "account_balances": [],
        "category_aggregates": [],
        "currencies": [],
    }
    assert set(legacy_comparison.json()["unavailable_sections"]) >= {
        "account_balances",
        "category_aggregates",
        "currencies",
    }
