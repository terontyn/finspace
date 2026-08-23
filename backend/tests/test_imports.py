import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.core.config import settings

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
