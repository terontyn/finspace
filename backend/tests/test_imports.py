import asyncio
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from app.core.config import settings
from app.db.models.imports import ImportBatch, ImportRow
from app.db.models.users import WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.services import (
    categorization_apply,
    categorization_executor,
    categorization_matcher,
    categorization_previews,
    categorization_rules,
)

PASSWORD = "correct horse battery staple"


def _auth(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], str]:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "import_storage_path", Path("/tmp/finspace-test-imports"))
    email = f"import-{uuid.uuid4()}@example.com"
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Import Test",
            "password": PASSWORD,
            "workspace_name": "Import Workspace",
            "base_currency": "RUB",
            "timezone": "Europe/Amsterdam",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return {
        "Authorization": f"Bearer {payload['access_token']}",
        "X-Workspace-ID": payload["workspace"]["id"],
    }, payload["workspace"]["id"]


def _references(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    account_name = f"Import Account {uuid.uuid4().hex[:8]}"
    category_name = f"Import Category {uuid.uuid4().hex[:8]}"
    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": account_name,
            "account_type": "debit_card",
            "currency": "RUB",
            "opening_balance": "0.0000",
            "opening_balance_at": "2026-01-01T00:00:00Z",
        },
    )
    category = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": category_name, "category_type": "expense"},
    )
    assert account.status_code == category.status_code == 201
    return account_name, category_name


def _csv(account: str, category: str, *, amount: str = "1 234,56") -> bytes:
    return (
        "Дата;Тип;Сумма;Счёт;Категория;Описание\n"
        f"22.07.2026;Расход;{amount};{account};{category};Тестовый импорт\n"
    ).encode()


def _upload(client: TestClient, headers: dict[str, str], content: bytes, name: str = "test.csv"):
    return client.post(
        "/api/v1/imports",
        headers=headers,
        files={"file": (name, content, "application/octet-stream")},
    )


def _mapping() -> dict[str, object]:
    return {
        "mapping": {
            "date": "Дата",
            "transaction_type": "Тип",
            "amount": "Сумма",
            "account": "Счёт",
            "category": "Категория",
            "description": "Описание",
        },
        "locale": "ru-RU",
    }


def _close_july(client: TestClient, headers: dict[str, str]) -> None:
    prepared = client.post("/api/v1/month-close/2026/7/prepare", headers=headers, json={})
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["status"] == "ready", prepared.text
    confirmed = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": f"import-close-{uuid.uuid4()}"},
        json={
            "version": prepared.json()["version"],
            "confirm": True,
            "prepare_token": prepared.json()["prepare_token"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text


async def _seed_ready_mixed_type_import(
    workspace_id: str,
    account_id: str,
    target_account_id: str,
    category_id: str,
) -> str:
    async with AsyncSessionFactory() as session:
        member = await session.scalar(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == uuid.UUID(workspace_id))
            .order_by(WorkspaceMember.created_at)
        )
        assert member is not None
        batch = ImportBatch(
            workspace_id=uuid.UUID(workspace_id),
            created_by=member.user_id,
            filename="stage-d-counts.csv",
            stored_filename=f"{uuid.uuid4().hex}.csv",
            file_type="csv",
            file_size=1,
            file_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status="ready",
            summary={"total": 5, "valid": 5, "invalid": 0, "duplicate": 0, "skipped": 0},
        )
        session.add(batch)
        await session.flush()
        rows = [
            ("expense", category_id, None),
            ("expense", None, None),
            ("transfer", None, target_account_id),
            ("adjustment", None, None),
            ("adjustment", None, None),
        ]
        for index, (transaction_type, row_category_id, row_target_id) in enumerate(rows, start=1):
            session.add(
                ImportRow(
                    batch_id=batch.id,
                    source_row_number=index + 1,
                    raw_data={"row": index},
                    normalized_data={
                        "workspace_id": workspace_id,
                        "occurred_at": datetime(2026, 8, index, 12, 0, tzinfo=UTC).isoformat(),
                        "transaction_type": transaction_type,
                        "amount": "10.0000",
                        "currency": "RUB",
                        "account_id": account_id,
                        "target_account_id": row_target_id,
                        "category_id": row_category_id,
                        "counterparty": f"Stage D {transaction_type}",
                        "description": None,
                        "comment": None,
                        "status": "confirmed",
                        "external_id": None,
                    },
                    status="valid",
                )
            )
        await session.commit()
        return str(batch.id)


def test_import_commit_persists_factual_review_counts_without_categorization_calls(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, workspace_id = _auth(client, monkeypatch)
    account_name, category_name = _references(client, headers)
    account = next(
        item
        for item in client.get("/api/v1/accounts?limit=200", headers=headers).json()["items"]
        if item["name"] == account_name
    )
    category = next(
        item
        for item in client.get("/api/v1/categories?limit=200", headers=headers).json()["items"]
        if item["name"] == category_name
    )
    target = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": f"Stage D target {uuid.uuid4().hex[:8]}",
            "account_type": "debit_card",
            "currency": "RUB",
            "opening_balance": "0.0000",
            "opening_balance_at": "2026-01-01T00:00:00Z",
        },
    ).json()
    batch_id = asyncio.run(
        _seed_ready_mixed_type_import(
            workspace_id,
            account["id"],
            target["id"],
            category["id"],
        )
    )

    def categorization_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("categorization service called during import commit")

    monkeypatch.setattr(categorization_matcher, "prepare_rule_set", categorization_must_not_run)
    monkeypatch.setattr(categorization_previews, "create_preview", categorization_must_not_run)
    monkeypatch.setattr(categorization_rules, "apply_to_transaction", categorization_must_not_run)
    monkeypatch.setattr(categorization_apply, "apply_preview_items", categorization_must_not_run)
    monkeypatch.setattr(categorization_executor, "execute_apply", categorization_must_not_run)

    key = str(uuid.uuid4())
    committed = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        headers={**headers, "X-Idempotency-Key": key},
        json={"confirm": True},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["affected_transactions"] == 5
    summary = committed.json()["batch"]["summary"]
    assert summary["affected_transactions"] == 5
    assert summary["uncategorized_at_commit"] == 4
    assert summary["review_candidates_at_commit"] == 3

    replayed = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        headers={**headers, "X-Idempotency-Key": key},
        json={"confirm": True},
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["batch"]["summary"] == summary

    imported = [
        item
        for item in client.get("/api/v1/transactions?limit=200", headers=headers).json()["items"]
        if item["source"] == "import"
    ]
    assert len(imported) == 5
    categorized = next(
        item for item in imported if item["transaction_type"] == "expense" and item["category"]
    )
    assert categorized["category"]["id"] == category["id"]


def test_csv_staging_validate_commit_duplicate_and_rollback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = _auth(client, monkeypatch)
    account_name, category_name = _references(client, headers)
    content = _csv(account_name, category_name)
    before = client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]

    uploaded = _upload(client, headers, content)
    assert uploaded.status_code == 201, uploaded.text
    batch_id = uploaded.json()["id"]
    assert uploaded.json()["status"] == "mapping_required"
    assert client.get("/api/v1/transactions", headers=headers).json()["page"]["total"] == before

    without_mapping = client.post(f"/api/v1/imports/{batch_id}/validate", headers=headers)
    assert without_mapping.status_code == 409
    assert without_mapping.json()["error"]["code"] == "IMPORT_MAPPING_INVALID"

    mapped = client.put(f"/api/v1/imports/{batch_id}/mapping", headers=headers, json=_mapping())
    assert mapped.status_code == 200
    validated = client.post(f"/api/v1/imports/{batch_id}/validate", headers=headers)
    assert validated.status_code == 200, validated.text
    assert validated.json()["summary"]["valid"] == 1
    assert validated.json()["summary"]["invalid"] == 0
    assert client.get("/api/v1/transactions", headers=headers).json()["page"]["total"] == before

    key = str(uuid.uuid4())
    committed = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        headers={**headers, "X-Idempotency-Key": key},
        json={"confirm": True},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["affected_transactions"] == 1
    repeated = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        headers={**headers, "X-Idempotency-Key": key},
        json={"confirm": True},
    )
    assert repeated.status_code == 200
    remap_after_commit = client.put(
        f"/api/v1/imports/{batch_id}/mapping", headers=headers, json=_mapping()
    )
    assert remap_after_commit.status_code == 409
    assert remap_after_commit.json()["error"]["code"] == "IMPORT_NOT_READY"
    transactions = client.get("/api/v1/transactions", headers=headers).json()
    assert transactions["page"]["total"] == before + 1
    imported = next(item for item in transactions["items"] if item["source"] == "import")
    assert imported["payee"] is None

    duplicate_upload = client.post(
        "/api/v1/imports",
        headers=headers,
        data={"force_duplicate": "true"},
        files={"file": ("again.csv", content, "text/csv")},
    )
    duplicate_id = duplicate_upload.json()["id"]
    client.put(f"/api/v1/imports/{duplicate_id}/mapping", headers=headers, json=_mapping())
    duplicate_validation = client.post(f"/api/v1/imports/{duplicate_id}/validate", headers=headers)
    assert duplicate_validation.json()["summary"]["duplicate"] == 1
    blocked_duplicate_commit = client.post(
        f"/api/v1/imports/{duplicate_id}/commit",
        headers={**headers, "X-Idempotency-Key": str(uuid.uuid4())},
        json={"confirm": True},
    )
    assert blocked_duplicate_commit.status_code == 409
    duplicate_rows = client.get(
        f"/api/v1/imports/{duplicate_id}/rows?duplicate=true", headers=headers
    ).json()
    override = client.patch(
        f"/api/v1/imports/{duplicate_id}/rows/{duplicate_rows['items'][0]['id']}",
        headers=headers,
        json={"import_as_new": True},
    )
    assert override.status_code == 200
    assert override.json()["status"] == "valid"
    refreshed_duplicate = client.get(f"/api/v1/imports/{duplicate_id}", headers=headers)
    assert refreshed_duplicate.json()["status"] == "ready"

    audit = client.get(
        f"/api/v1/audit?entity_type=import_batch&entity_id={batch_id}", headers=headers
    )
    assert {item["action"] for item in audit.json()["items"]} >= {
        "import.upload",
        "import.mapping",
        "import.validate",
        "import.commit",
    }
    rolled_back = client.post(
        f"/api/v1/imports/{batch_id}/rollback", headers=headers, json={"force": False}
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["affected_transactions"] == 1
    assert (
        client.post(
            f"/api/v1/imports/{batch_id}/rollback", headers=headers, json={"force": False}
        ).status_code
        == 200
    )
    deleted = client.get(f"/api/v1/transactions/{imported['id']}", headers=headers)
    assert deleted.status_code == 404


def test_import_rollback_never_removes_reconciled_transaction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = _auth(client, monkeypatch)
    account_name, category_name = _references(client, headers)
    batch = _upload(client, headers, _csv(account_name, category_name)).json()
    batch_id = batch["id"]
    mapped = client.put(f"/api/v1/imports/{batch_id}/mapping", headers=headers, json=_mapping())
    assert mapped.status_code == 200, mapped.text
    validated = client.post(f"/api/v1/imports/{batch_id}/validate", headers=headers)
    assert validated.status_code == 200, validated.text
    committed = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        headers={**headers, "X-Idempotency-Key": str(uuid.uuid4())},
        json={"confirm": True},
    )
    assert committed.status_code == 200, committed.text

    accounts = client.get("/api/v1/accounts", headers=headers).json()["items"]
    account = next(item for item in accounts if item["name"] == account_name)
    transactions = client.get("/api/v1/transactions", headers=headers).json()["items"]
    imported = next(item for item in transactions if item["source"] == "import")
    preview = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/preview",
        headers=headers,
        json={
            "statement_date": "2026-07-31",
            "statement_balance": "-1234.5600",
            "currency": "RUB",
            "account_version": account["version"],
        },
    )
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    confirmed = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json={
            "statement_date": preview_payload["statement_date"],
            "statement_balance": preview_payload["statement_balance"],
            "currency": preview_payload["currency"],
            "account_version": account["version"],
            "preview_token": preview_payload["preview_token"],
            "idempotency_key": f"import-reconciliation-{uuid.uuid4()}",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["transaction_ids"] == [imported["id"]]

    transaction_before = client.get(
        f"/api/v1/transactions/{imported['id']}", headers=headers
    ).json()
    history_before = client.get(
        f"/api/v1/accounts/{account['id']}/reconciliations", headers=headers
    ).json()

    for force in (False, True):
        rolled_back = client.post(
            f"/api/v1/imports/{batch_id}/rollback",
            headers=headers,
            json={"force": force},
        )
        assert rolled_back.status_code == 409, rolled_back.text
        error = rolled_back.json()["error"]
        assert error["code"] == "IMPORT_ROLLBACK_RECONCILED_CONFLICT"
        assert error["details"] == {"transaction_ids": [imported["id"]]}
        assert (
            client.get(f"/api/v1/transactions/{imported['id']}", headers=headers).json()
            == transaction_before
        )
        assert (
            client.get(f"/api/v1/accounts/{account['id']}/reconciliations", headers=headers).json()
            == history_before
        )


def test_import_commit_and_rollback_respect_closed_period_atomically(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = _auth(client, monkeypatch)
    account_name, category_name = _references(client, headers)
    mixed = (
        "Дата;Тип;Сумма;Счёт;Категория;Описание\n"
        f"22.07.2026;Расход;10;{account_name};{category_name};Closed row\n"
        f"22.08.2026;Расход;20;{account_name};{category_name};Open row\n"
    ).encode()
    batch = _upload(client, headers, mixed, "closed-mixed.csv").json()
    client.put(f"/api/v1/imports/{batch['id']}/mapping", headers=headers, json=_mapping())
    validated = client.post(f"/api/v1/imports/{batch['id']}/validate", headers=headers)
    assert validated.status_code == 200, validated.text
    assert validated.json()["summary"]["valid"] == 2
    _close_july(client, headers)

    before = client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]
    rejected = client.post(
        f"/api/v1/imports/{batch['id']}/commit",
        headers={**headers, "X-Idempotency-Key": f"closed-import-{uuid.uuid4()}"},
        json={"confirm": True},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "MONTH_CLOSED"
    assert client.get("/api/v1/transactions", headers=headers).json()["page"]["total"] == before
    assert client.get(f"/api/v1/imports/{batch['id']}", headers=headers).json()["status"] == "ready"

    # A separate workspace proves rollback performs the same preflight before
    # changing any imported row, audit event, or outbox record.
    rollback_headers, _ = _auth(client, monkeypatch)
    rollback_account, rollback_category = _references(client, rollback_headers)
    rollback_batch = _upload(
        client,
        rollback_headers,
        _csv(rollback_account, rollback_category),
        "rollback-closed.csv",
    ).json()
    client.put(
        f"/api/v1/imports/{rollback_batch['id']}/mapping",
        headers=rollback_headers,
        json=_mapping(),
    )
    client.post(f"/api/v1/imports/{rollback_batch['id']}/validate", headers=rollback_headers)
    committed = client.post(
        f"/api/v1/imports/{rollback_batch['id']}/commit",
        headers={**rollback_headers, "X-Idempotency-Key": f"rollback-import-{uuid.uuid4()}"},
        json={"confirm": True},
    )
    assert committed.status_code == 200, committed.text
    imported = next(
        item
        for item in client.get("/api/v1/transactions", headers=rollback_headers).json()["items"]
        if item["source"] == "import"
    )
    _close_july(client, rollback_headers)
    rollback = client.post(
        f"/api/v1/imports/{rollback_batch['id']}/rollback",
        headers=rollback_headers,
        json={"force": True},
    )
    assert rollback.status_code == 409, rollback.text
    assert rollback.json()["error"]["code"] == "MONTH_CLOSED"
    imported_after_rejection = client.get(
        f"/api/v1/transactions/{imported['id']}", headers=rollback_headers
    )
    assert imported_after_rejection.status_code == 200
    assert (
        client.get(f"/api/v1/imports/{rollback_batch['id']}", headers=rollback_headers).json()[
            "status"
        ]
        == "imported"
    )


def test_import_validation_errors_limits_and_workspace_isolation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = _auth(client, monkeypatch)
    other_headers, _ = _auth(client, monkeypatch)
    account_name, category_name = _references(client, headers)
    content = _csv(account_name, category_name)
    batch = _upload(client, headers, content).json()
    assert client.get(f"/api/v1/imports/{batch['id']}", headers=other_headers).status_code == 404

    duplicate_file = _upload(client, headers, content)
    assert duplicate_file.status_code == 409
    assert duplicate_file.json()["error"]["code"] == "IMPORT_DUPLICATE_FILE"
    forbidden = _upload(client, headers, b"not a workbook", "macro.xlsm")
    assert forbidden.status_code == 415
    assert forbidden.json()["error"]["code"] == "IMPORT_FILE_TYPE_NOT_ALLOWED"

    unknown = _upload(client, headers, _csv("Missing", "Missing", amount="10,50"), "unknown.csv")
    unknown_id = unknown.json()["id"]
    client.put(f"/api/v1/imports/{unknown_id}/mapping", headers=headers, json=_mapping())
    invalid = client.post(f"/api/v1/imports/{unknown_id}/validate", headers=headers)
    assert invalid.json()["summary"]["invalid"] == 1
    rows = client.get(f"/api/v1/imports/{unknown_id}/rows?has_errors=true", headers=headers).json()
    assert rows["page"]["total"] == 1

    monkeypatch.setattr(settings, "import_max_rows", 1)
    too_many = (
        "Дата;Тип;Сумма;Счёт;Категория;Описание\n"
        f"22.07.2026;Расход;1;{account_name};{category_name};A\n"
        f"23.07.2026;Расход;2;{account_name};{category_name};B\n"
    ).encode()
    limited = _upload(client, headers, too_many, "too-many.csv")
    assert limited.status_code == 413
    assert limited.json()["error"]["code"] == "IMPORT_ROW_LIMIT_EXCEEDED"

    monkeypatch.setattr(settings, "import_max_rows", 100000)
    monkeypatch.setattr(settings, "import_max_file_size_mb", 1)
    oversized = _upload(client, headers, b"x" * (1024 * 1024 + 1), "oversized.csv")
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "IMPORT_FILE_TOO_LARGE"


def test_xlsx_is_read_only_and_formulas_are_not_executed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = _auth(client, monkeypatch)
    account_name, category_name = _references(client, headers)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Дата", "Тип", "Сумма", "Счёт", "Категория", "Описание"])
    sheet.append(["22.07.2026", "Расход", "=1+1", account_name, category_name, "Formula"])
    second_sheet = workbook.create_sheet("Second")
    second_sheet.append(["Дата", "Тип", "Сумма", "Счёт", "Категория", "Описание"])
    second_sheet.append(
        ["23.07.2026", "Расход", "=2+2", account_name, category_name, "Formula two"]
    )
    output = io.BytesIO()
    workbook.save(output)
    uploaded = _upload(client, headers, output.getvalue(), "formula.xlsx")
    assert uploaded.status_code == 201, uploaded.text
    batch_id = uploaded.json()["id"]
    client.put(f"/api/v1/imports/{batch_id}/mapping", headers=headers, json=_mapping())
    validated = client.post(f"/api/v1/imports/{batch_id}/validate", headers=headers)
    assert validated.json()["summary"]["invalid"] == 2


def test_import_locale_empty_rows_domain_validation_and_currency_isolation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, _ = _auth(client, monkeypatch)
    rub_account, expense_category = _references(client, headers)
    eur_account = f"EUR Import {uuid.uuid4().hex[:8]}"
    created_eur = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": eur_account,
            "account_type": "debit_card",
            "currency": "EUR",
            "opening_balance": "0.0000",
            "opening_balance_at": "2026-01-01T00:00:00Z",
        },
    )
    assert created_eur.status_code == 201, created_eur.text

    content = (
        "Date;Type;Amount;Currency;Account;Category;Description\n"
        f"08/05/2026;Expense;10.25;RUB;{rub_account};{expense_category};Lunch\n"
        f"08/05/2026;Expense;10.25;RUB;{rub_account};{expense_category};Lunch\n"
        f"08/06/2026;Expense;invalid;RUB;{rub_account};{expense_category};Bad amount\n"
        ";;;;;;\n"
        f"08/07/2026;Income;40;EUR;{eur_account};;Salary\n"
        f"08/08/2026;Income;5;RUB;{rub_account};{expense_category};Wrong category\n"
        f"08/09/2026;Expense;-1;RUB;{rub_account};{expense_category};Negative\n"
    ).encode()
    uploaded = _upload(client, headers, content, "mixed.csv")
    assert uploaded.status_code == 201, uploaded.text
    batch_id = uploaded.json()["id"]

    incomplete = client.put(
        f"/api/v1/imports/{batch_id}/mapping",
        headers=headers,
        json={
            "mapping": {"date": "Date", "amount": "Amount", "account": "Account"},
            "locale": "en-US",
        },
    )
    assert incomplete.status_code == 422
    assert "transaction_type" in incomplete.json()["error"]["details"]["required"]

    mapping = {
        "mapping": {
            "date": "Date",
            "transaction_type": "Type",
            "amount": "Amount",
            "currency": "Currency",
            "account": "Account",
            "category": "Category",
            "description": "Description",
        },
        "locale": "en-US",
    }
    mapped = client.put(
        f"/api/v1/imports/{batch_id}/mapping",
        headers=headers,
        json=mapping,
    )
    assert mapped.status_code == 200, mapped.text
    validated = client.post(f"/api/v1/imports/{batch_id}/validate", headers=headers)
    assert validated.status_code == 200, validated.text
    summary = validated.json()["summary"]
    assert summary["valid"] == 2
    assert summary["duplicate"] == 1
    assert summary["invalid"] == 3
    assert summary["skipped"] == 1
    assert summary["currencies"] == ["EUR", "RUB"]
    assert summary["date_from"] == "2026-08-05"
    assert summary["date_to"] == "2026-08-07"
    assert "Date" in summary["source_columns"]

    invalid_rows = client.get(
        f"/api/v1/imports/{batch_id}/rows?has_errors=true",
        headers=headers,
    ).json()["items"]
    errors = " ".join(
        error["message"] for row in invalid_rows for error in row["validation_errors"]
    )
    assert "decimal" in errors
    assert "Category type" in errors
    assert "positive" in errors

    key = str(uuid.uuid4())
    committed = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        headers={**headers, "X-Idempotency-Key": key},
        json={"confirm": True},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["affected_transactions"] == 2
    repeated = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        headers={**headers, "X-Idempotency-Key": key},
        json={"confirm": True},
    )
    assert repeated.status_code == 200
    assert repeated.json()["affected_transactions"] == 2

    transactions = client.get("/api/v1/transactions?limit=100", headers=headers).json()["items"]
    imported = [item for item in transactions if item["source"] == "import"]
    assert len(imported) == 2
    rub_expense = next(item for item in imported if item["currency"] == "RUB")
    assert rub_expense["occurred_at"].startswith("2026-08-04T22:00:00")

    report = client.get(
        "/api/v1/reports/financial?date_from=2026-08-01&date_to=2026-08-31",
        headers=headers,
    )
    assert report.status_code == 200, report.text
    groups = {item["currency"]: item for item in report.json()["groups"]}
    assert groups["RUB"]["expense"] == "10.2500"
    assert groups["RUB"]["net_cashflow"] == "-10.2500"
    assert groups["EUR"]["income"] == "40.0000"
    assert groups["EUR"]["net_cashflow"] == "40.0000"
