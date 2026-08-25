import asyncio
import runpy
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.audit import AuditLog
from app.db.models.automations import MonthCloseControl, MonthCloseRevision, MonthClosure
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.dependencies.context import RequestContext
from app.schemas.automations import BackupStatusResponse
from app.schemas.transactions import TransactionCreate, TransactionUpdate
from app.services import imports as import_service
from app.services import month_close as month_close_service
from app.services import transactions as transaction_service
from app.services.month_close_fingerprint import (
    canonical_decimal,
    canonical_json,
    financial_fingerprint,
)
from tests.test_automations import _register


@pytest.fixture(autouse=True)
def _configure_month_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _account(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    currency: str = "RUB",
    opening_balance: str = "100.0000",
    opening_balance_at: str = "2026-01-01T00:00:00Z",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": name or f"Month close {uuid.uuid4().hex[:8]}",
            "account_type": "cash",
            "currency": currency,
            "opening_balance": opening_balance,
            "opening_balance_at": opening_balance_at,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _category(
    client: TestClient,
    headers: dict[str, str],
    category_type: str,
    *,
    name: str | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/categories",
        headers=headers,
        json={
            "name": name or f"Month close {category_type} {uuid.uuid4().hex[:8]}",
            "category_type": category_type,
            "color": "#00aa88",
            "icon": "wallet",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _transaction(
    client: TestClient,
    headers: dict[str, str],
    *,
    account_id: str,
    occurred_at: str = "2026-07-10T10:00:00Z",
    transaction_type: str = "income",
    amount: str = "10.0000",
    currency: str = "RUB",
    category_id: str | None = None,
    target_account_id: str | None = None,
    related_transaction_id: str | None = None,
    status: str = "confirmed",
    comment: str | None = None,
    splits: list[dict[str, str]] | None = None,
) -> object:
    return client.post(
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
            "comment": comment,
            "splits": splits or [],
        },
    )


def _prepare(client: TestClient, headers: dict[str, str], year: int, month: int) -> object:
    return client.post(f"/api/v1/month-close/{year}/{month}/prepare", headers=headers, json={})


def _confirm(
    client: TestClient,
    headers: dict[str, str],
    prepared: dict[str, object],
    year: int,
    month: int,
    *,
    key: str | None = None,
) -> object:
    return client.post(
        f"/api/v1/month-close/{year}/{month}/confirm",
        headers={**headers, "X-Idempotency-Key": key or f"confirm-{uuid.uuid4()}"},
        json={
            "version": prepared["version"],
            "confirm": True,
            "prepare_token": prepared["prepare_token"],
        },
    )


def _ready_import_batch(
    client: TestClient,
    headers: dict[str, str],
    *,
    account_name: str,
    category_name: str,
) -> str:
    content = (
        "Дата;Тип;Сумма;Счёт;Категория;Описание\n"
        f"20.07.2026;Расход;10;{account_name};{category_name};Bulk row one\n"
        f"21.07.2026;Расход;20;{account_name};{category_name};Bulk row two\n"
    ).encode()
    uploaded = client.post(
        "/api/v1/imports",
        headers=headers,
        files={"file": (f"month-close-race-{uuid.uuid4()}.csv", content, "text/csv")},
    )
    assert uploaded.status_code == 201, uploaded.text
    batch_id = str(uploaded.json()["id"])
    mapped = client.put(
        f"/api/v1/imports/{batch_id}/mapping",
        headers=headers,
        json={
            "mapping": {
                "date": "Дата",
                "transaction_type": "Тип",
                "amount": "Сумма",
                "account": "Счёт",
                "category": "Категория",
                "description": "Описание",
            },
            "locale": "ru-RU",
        },
    )
    assert mapped.status_code == 200, mapped.text
    validated = client.post(f"/api/v1/imports/{batch_id}/validate", headers=headers)
    assert validated.status_code == 200, validated.text
    assert validated.json()["summary"]["valid"] == 2
    return batch_id


def _close(
    client: TestClient,
    headers: dict[str, str],
    year: int = 2026,
    month: int = 7,
) -> dict[str, object]:
    prepared = _prepare(client, headers, year, month)
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["status"] == "ready", prepared.text
    confirmed = _confirm(client, headers, prepared.json(), year, month)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    return confirmed.json()


def _force_missing_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing_backup(*_args: object, **_kwargs: object) -> BackupStatusResponse:
        return BackupStatusResponse(
            status="missing",
            last_backup_at=None,
            last_verified_at=None,
            revision=None,
            age_hours=None,
            sha256_short=None,
            stale=True,
            warning="Test fixture: backup is missing.",
        )

    monkeypatch.setattr(month_close_service, "get_backup_status", missing_backup)


def _dev_headers(identity: dict[str, object]) -> dict[str, str]:
    return {
        "X-User-ID": str(identity["user"]["id"]),
        "X-Workspace-ID": str(identity["workspace"]["id"]),
    }


def _role_headers(identity: dict[str, object], role: str) -> dict[str, str]:
    async def create_member() -> dict[str, str]:
        async with AsyncSessionFactory() as session:
            unique = uuid.uuid4().hex
            user = User(
                email=f"month-close-{role}-{unique}@test.local",
                normalized_email=f"month-close-{role}-{unique}@test.local",
                display_name=f"Month close {role}",
                timezone="UTC",
            )
            session.add(user)
            await session.flush()
            session.add(
                WorkspaceMember(
                    workspace_id=uuid.UUID(str(identity["workspace"]["id"])),
                    user_id=user.id,
                    role=role,
                )
            )
            await session.commit()
            return {
                "X-User-ID": str(user.id),
                "X-Workspace-ID": str(identity["workspace"]["id"]),
            }

    return asyncio.run(create_member())


async def _database_counts(workspace_id: str) -> dict[str, int]:
    workspace_uuid = uuid.UUID(workspace_id)
    async with AsyncSessionFactory() as session:
        return {
            "closures": int(
                await session.scalar(
                    select(func.count())
                    .select_from(MonthClosure)
                    .where(MonthClosure.workspace_id == workspace_uuid)
                )
                or 0
            ),
            "revisions": int(
                await session.scalar(
                    select(func.count())
                    .select_from(MonthCloseRevision)
                    .where(MonthCloseRevision.workspace_id == workspace_uuid)
                )
                or 0
            ),
        }


def test_state_machine_roles_completed_period_and_confirm_idempotency(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_missing_backup(monkeypatch)
    identity, owner_headers = _register(client, "Month Close State")
    viewer_headers = _role_headers(identity, "viewer")
    editor_headers = _role_headers(identity, "editor")

    future = _prepare(client, owner_headers, 2026, 8)
    assert future.status_code == 409
    assert future.json()["error"]["code"] == "MONTH_CLOSE_PERIOD_NOT_ENDED"

    viewer = _prepare(client, viewer_headers, 2026, 7)
    assert viewer.status_code == 403
    assert viewer.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    prepared = _prepare(client, editor_headers, 2026, 7)
    assert prepared.status_code == 200, prepared.text
    preview = prepared.json()
    assert preview["status"] == "ready"
    assert len(preview["prepare_token"]) == 64
    assert len(preview["prepared_fingerprint"]) == 64
    assert any(item["code"].startswith("BACKUP_") for item in preview["warning_issues"])

    missing_key = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers=owner_headers,
        json={
            "version": preview["version"],
            "confirm": True,
            "prepare_token": preview["prepare_token"],
        },
    )
    assert missing_key.status_code == 422

    key = f"confirm-{uuid.uuid4()}"
    first = _confirm(client, owner_headers, preview, 2026, 7, key=key)
    second = _confirm(client, owner_headers, preview, 2026, 7, key=key)
    assert first.status_code == second.status_code == 200
    assert first.json()["current_revision_id"] == second.json()["current_revision_id"]
    assert asyncio.run(_database_counts(str(identity["workspace"]["id"])))["revisions"] == 1

    changed_payload = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**owner_headers, "X-Idempotency-Key": key},
        json={
            "version": int(preview["version"]) + 1,
            "confirm": True,
            "prepare_token": preview["prepare_token"],
        },
    )
    assert changed_payload.status_code == 409
    assert changed_payload.json()["error"]["code"] == "MONTH_CLOSE_IDEMPOTENCY_CONFLICT"

    prepare_confirmed = _prepare(client, editor_headers, 2026, 7)
    assert prepare_confirmed.status_code == 409
    assert prepare_confirmed.json()["error"]["code"] == "MONTH_ALREADY_CLOSED"


def test_draft_and_backup_policy_blockers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_missing_backup(monkeypatch)
    identity, headers = _register(client, "Month Close Blockers")
    account = _account(client, headers)
    category = _category(client, headers, "expense")
    draft = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        transaction_type="expense",
        category_id=str(category["id"]),
        status="draft",
    )
    assert draft.status_code == 201
    blocked = _prepare(client, headers, 2026, 7)
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"
    assert {item["code"] for item in blocked.json()["blocking_issues"]} >= {"DRAFT_TRANSACTIONS"}

    confirmed = client.post(
        f"/api/v1/transactions/{draft.json()['id']}/confirm",
        headers=headers,
        json={"version": draft.json()["version"]},
    )
    assert confirmed.status_code == 200

    async def require_backup() -> None:
        async with AsyncSessionFactory() as session:
            control = await session.get(
                MonthCloseControl, uuid.UUID(str(identity["workspace"]["id"]))
            )
            assert control is not None
            control.backup_policy = "require_healthy"
            control.version += 1
            await session.commit()

    asyncio.run(require_backup())
    backup_blocked = _prepare(client, headers, 2026, 7)
    assert backup_blocked.status_code == 200
    assert backup_blocked.json()["status"] == "blocked"
    assert any(
        item["code"].startswith("BACKUP_") for item in backup_blocked.json()["blocking_issues"]
    )


def test_transaction_guard_covers_dates_lifecycle_transfer_split_and_refund(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Month Close Transactions")
    source = _account(client, headers)
    target = _account(client, headers, opening_balance="0")
    income_category = _category(client, headers, "income")
    expense_category = _category(client, headers, "expense")
    split_category = _category(client, headers, "expense")

    old_for_update = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        category_id=str(income_category["id"]),
    ).json()
    old_for_cancel = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        category_id=str(income_category["id"]),
        occurred_at="2026-07-11T10:00:00Z",
    ).json()
    old_for_delete = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        category_id=str(income_category["id"]),
        occurred_at="2026-07-12T10:00:00Z",
    ).json()
    old_deleted = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        category_id=str(income_category["id"]),
        occurred_at="2026-07-13T10:00:00Z",
    ).json()
    deleted_before_close = client.delete(
        f"/api/v1/transactions/{old_deleted['id']}",
        headers=headers,
        params={"version": old_deleted["version"]},
    )
    assert deleted_before_close.status_code == 200
    closed = _close(client, headers)
    assert closed["period_month"] == "2026-07-01"

    for occurred_at in ("2026-07-20T00:00:00Z", "2026-06-20T00:00:00Z"):
        response = _transaction(
            client,
            headers,
            account_id=str(source["id"]),
            category_id=str(income_category["id"]),
            occurred_at=occurred_at,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MONTH_CLOSED"

    open_transaction = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        category_id=str(income_category["id"]),
        occurred_at="2026-08-10T10:00:00Z",
    )
    assert open_transaction.status_code == 201, open_transaction.text
    open_row = open_transaction.json()

    old_to_new = client.patch(
        f"/api/v1/transactions/{old_for_update['id']}",
        headers=headers,
        json={"version": old_for_update["version"], "occurred_at": "2026-08-11T10:00:00Z"},
    )
    new_to_old = client.patch(
        f"/api/v1/transactions/{open_row['id']}",
        headers=headers,
        json={"version": open_row["version"], "occurred_at": "2026-07-11T10:00:00Z"},
    )
    for response in (old_to_new, new_to_old):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MONTH_CLOSED"

    cancel = client.post(
        f"/api/v1/transactions/{old_for_cancel['id']}/cancel",
        headers=headers,
        json={"version": old_for_cancel["version"]},
    )
    delete = client.delete(
        f"/api/v1/transactions/{old_for_delete['id']}",
        headers=headers,
        params={"version": old_for_delete["version"]},
    )
    restore = client.post(
        f"/api/v1/transactions/{old_deleted['id']}/restore",
        headers=headers,
        json={"version": deleted_before_close.json()["version"]},
    )
    for response in (cancel, delete, restore):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MONTH_CLOSED"

    transfer = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        target_account_id=str(target["id"]),
        transaction_type="transfer",
        occurred_at="2026-07-14T10:00:00Z",
    )
    split = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        transaction_type="expense",
        amount="10.0000",
        occurred_at="2026-07-15T10:00:00Z",
        splits=[
            {"category_id": str(expense_category["id"]), "amount": "4.0000"},
            {"category_id": str(split_category["id"]), "amount": "6.0000"},
        ],
    )
    refund = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        transaction_type="refund",
        related_transaction_id=str(old_for_update["id"]),
        occurred_at="2026-07-16T10:00:00Z",
    )
    for response in (transfer, split, refund):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MONTH_CLOSED"


def test_account_guard_and_cosmetic_account_category_changes(client: TestClient) -> None:
    _, headers = _register(client, "Month Close Accounts")
    account = _account(client, headers, opening_balance="100")
    restorable = _account(client, headers, opening_balance="50")
    zero_account = _account(client, headers, opening_balance="0")
    zero_restorable = _account(client, headers, opening_balance="0")
    category = _category(client, headers, "expense")
    removed = client.delete(
        f"/api/v1/accounts/{restorable['id']}",
        headers=headers,
        params={"version": restorable["version"]},
    )
    zero_removed = client.delete(
        f"/api/v1/accounts/{zero_restorable['id']}",
        headers=headers,
        params={"version": zero_restorable["version"]},
    )
    assert removed.status_code == 200
    assert zero_removed.status_code == 200
    _close(client, headers)

    current = client.get(f"/api/v1/accounts/{account['id']}", headers=headers).json()
    renamed = client.patch(
        f"/api/v1/accounts/{account['id']}",
        headers=headers,
        json={
            "version": current["version"],
            "name": "Cosmetic rename after close",
            "institution": "Cosmetic bank label",
            "description": "Cosmetic description",
        },
    )
    assert renamed.status_code == 200, renamed.text
    archived = client.patch(
        f"/api/v1/accounts/{account['id']}",
        headers=headers,
        json={"version": renamed.json()["version"], "is_archived": True},
    )
    assert archived.status_code == 200, archived.text

    for change in (
        {"opening_balance": "101.0000"},
        {"opening_balance_at": "2026-02-01T00:00:00Z"},
        {"currency": "USD"},
    ):
        response = client.patch(
            f"/api/v1/accounts/{account['id']}",
            headers=headers,
            json={"version": archived.json()["version"], **change},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MONTH_CLOSED"

    historical = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Forbidden historical account",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-31T00:00:00Z",
        },
    )
    future = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Open period account",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-08-01T00:00:00Z",
        },
    )
    assert historical.status_code == 409
    assert historical.json()["error"]["code"] == "MONTH_CLOSED"
    assert future.status_code == 201, future.text

    delete_account = client.delete(
        f"/api/v1/accounts/{account['id']}",
        headers=headers,
        params={"version": archived.json()["version"]},
    )
    restore_account = client.post(
        f"/api/v1/accounts/{restorable['id']}/restore",
        headers=headers,
        json={"version": removed.json()["version"]},
    )
    delete_zero_account = client.delete(
        f"/api/v1/accounts/{zero_account['id']}",
        headers=headers,
        params={"version": zero_account["version"]},
    )
    restore_zero_account = client.post(
        f"/api/v1/accounts/{zero_restorable['id']}/restore",
        headers=headers,
        json={"version": zero_removed.json()["version"]},
    )
    for response in (
        delete_account,
        restore_account,
        delete_zero_account,
        restore_zero_account,
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MONTH_CLOSED"

    category_update = client.patch(
        f"/api/v1/categories/{category['id']}",
        headers=headers,
        json={
            "version": category["version"],
            "name": "Historical label preserved in revision",
            "icon": "food",
            "color": "#abcdef",
            "sort_order": 9,
            "is_archived": True,
        },
    )
    assert category_update.status_code == 200, category_update.text


def test_account_currency_cannot_change_after_any_transaction_exists(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Account Currency Invariant")
    account = _account(client, headers, opening_balance="0")
    category = _category(client, headers, "income")
    transaction = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        category_id=str(category["id"]),
        occurred_at="2026-08-10T10:00:00Z",
    )
    assert transaction.status_code == 201, transaction.text

    changed = client.patch(
        f"/api/v1/accounts/{account['id']}",
        headers=headers,
        json={"version": account["version"], "currency": "USD"},
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "ACCOUNT_CURRENCY_IMMUTABLE"


def test_legacy_backfill_cutoff_requires_a_fully_proven_active_chain() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0008_month_close_invariants.py"
    )
    derive = runpy.run_path(str(migration_path))["_legacy_closed_through"]
    jan = date(2024, 1, 1)
    feb = date(2024, 2, 1)
    mar = date(2024, 3, 1)

    assert derive([]) is None
    assert derive(
        [
            {"period_month": jan, "status": "confirmed"},
            {"period_month": feb, "status": "confirmed"},
        ]
    ) == date(2024, 2, 29)
    assert (
        derive(
            [
                {"period_month": jan, "status": "reopened"},
                {"period_month": feb, "status": "confirmed"},
            ]
        )
        is None
    )
    assert (
        derive(
            [
                {"period_month": jan, "status": "confirmed"},
                {"period_month": feb, "status": "reopened"},
                {"period_month": mar, "status": "confirmed"},
            ]
        )
        is None
    )
    assert derive(
        [
            {"period_month": jan, "status": "confirmed"},
            {"period_month": feb, "status": "confirmed"},
            {"period_month": mar, "status": "reopened"},
        ]
    ) == date(2024, 2, 29)
    assert (
        derive(
            [
                {"period_month": jan, "status": "confirmed"},
                {"period_month": mar, "status": "confirmed"},
            ]
        )
        is None
    )


def test_ambiguous_legacy_confirmed_history_cannot_create_a_new_cutoff(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Ambiguous Legacy Month Close")

    async def seed_ambiguous_history() -> None:
        async with AsyncSessionFactory() as session:
            workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
            user_id = uuid.UUID(str(identity["user"]["id"]))
            session.add(
                MonthCloseControl(
                    workspace_id=workspace_id,
                    closed_through=None,
                    backup_policy="warn",
                    version=1,
                )
            )
            closure = MonthClosure(
                workspace_id=workspace_id,
                period_month=date(2026, 3, 1),
                status="confirmed",
                confirmed_by=user_id,
                confirmed_at=datetime.now(UTC),
                summary={"legacy_unverified": True},
                blocking_issues=[],
                warning_issues=[],
                version=1,
            )
            session.add(closure)
            await session.commit()

    asyncio.run(seed_ambiguous_history())
    prepared = _prepare(client, headers, 2026, 7)
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["status"] == "blocked"
    issue = next(
        item
        for item in prepared.json()["blocking_issues"]
        if item["code"] == "MONTH_CLOSE_SEQUENCE_CONFLICT"
    )
    assert issue["reason"] == "legacy_confirmed_history_is_ambiguous"

    async def cutoff() -> date | None:
        async with AsyncSessionFactory() as session:
            control = await session.get(
                MonthCloseControl, uuid.UUID(str(identity["workspace"]["id"]))
            )
            assert control is not None
            return control.closed_through

    assert asyncio.run(cutoff()) is None


def test_sequential_close_latest_reopen_and_immutable_revisions(client: TestClient) -> None:
    identity, headers = _register(client, "Month Close History")
    june = _close(client, headers, 2026, 6)

    out_of_sequence_preview = _prepare(client, headers, 2026, 5)
    assert out_of_sequence_preview.status_code == 200
    assert out_of_sequence_preview.json()["status"] == "blocked"
    assert any(
        item["code"] == "MONTH_CLOSE_SEQUENCE_CONFLICT"
        for item in out_of_sequence_preview.json()["blocking_issues"]
    )

    july = _close(client, headers, 2026, 7)
    reopen_june = client.post(
        "/api/v1/month-close/2026/6/reopen",
        headers={**headers, "X-Idempotency-Key": f"reopen-june-{uuid.uuid4()}"},
        json={"version": june["version"], "reason": "Out-of-order correction"},
    )
    assert reopen_june.status_code == 409
    assert reopen_june.json()["error"]["code"] == "MONTH_CLOSE_REOPEN_ORDER_CONFLICT"

    async def original_revision() -> tuple[uuid.UUID, dict[str, object]]:
        async with AsyncSessionFactory() as session:
            row = await session.get(MonthCloseRevision, uuid.UUID(str(july["current_revision_id"])))
            assert row is not None
            return row.id, dict(row.snapshot)

    original_revision_id, original_snapshot = asyncio.run(original_revision())
    reopen_key = f"reopen-july-{uuid.uuid4()}"
    reopened = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**headers, "X-Idempotency-Key": reopen_key},
        json={"version": july["version"], "reason": "Approved late correction"},
    )
    retry = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**headers, "X-Idempotency-Key": reopen_key},
        json={"version": july["version"], "reason": "Approved late correction"},
    )
    assert reopened.status_code == retry.status_code == 200
    assert reopened.json()["status"] == "reopened"
    assert retry.json()["version"] == reopened.json()["version"]
    assert reopened.json()["current_revision_id"] == str(original_revision_id)

    direct_confirm = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": f"direct-{uuid.uuid4()}"},
        json={
            "version": reopened.json()["version"],
            "confirm": True,
            "prepare_token": july["prepare_token"],
        },
    )
    assert direct_confirm.status_code == 409
    assert direct_confirm.json()["error"]["code"] == "MONTH_CLOSE_INVALID_STATE"

    reprepared = _prepare(client, headers, 2026, 7)
    assert reprepared.status_code == 200
    assert reprepared.json()["status"] == "ready"
    reclosed = _confirm(client, headers, reprepared.json(), 2026, 7)
    assert reclosed.status_code == 200, reclosed.text
    assert reclosed.json()["current_revision_id"] != str(original_revision_id)

    async def inspect_history() -> tuple[list[MonthCloseRevision], int, date | None]:
        async with AsyncSessionFactory() as session:
            revisions = list(
                (
                    await session.scalars(
                        select(MonthCloseRevision)
                        .where(
                            MonthCloseRevision.workspace_id
                            == uuid.UUID(str(identity["workspace"]["id"])),
                            MonthCloseRevision.period_month == date(2026, 7, 1),
                        )
                        .order_by(MonthCloseRevision.revision_number)
                    )
                ).all()
            )
            reopen_audits = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.workspace_id == uuid.UUID(str(identity["workspace"]["id"])),
                        AuditLog.action == "month_close.reopen",
                    )
                )
                or 0
            )
            control = await session.get(
                MonthCloseControl, uuid.UUID(str(identity["workspace"]["id"]))
            )
            assert control is not None
            return revisions, reopen_audits, control.closed_through

    revisions, reopen_audits, closed_through = asyncio.run(inspect_history())
    assert [item.revision_number for item in revisions] == [1, 2]
    assert revisions[0].snapshot == original_snapshot
    assert reopen_audits == 1
    assert closed_through == date(2026, 7, 31)


def test_idempotent_replays_return_original_result_after_later_state_changes(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Month Close Durable Idempotency")
    prepared = _prepare(client, headers, 2026, 7).json()
    confirm_key = f"confirm-original-{uuid.uuid4()}"
    confirmed = _confirm(client, headers, prepared, 2026, 7, key=confirm_key)
    assert confirmed.status_code == 200, confirmed.text
    confirmed_result = confirmed.json()

    reopen_key = f"reopen-original-{uuid.uuid4()}"
    reopen_payload = {
        "version": confirmed_result["version"],
        "reason": "Approved durable idempotency test",
    }
    reopened = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**headers, "X-Idempotency-Key": reopen_key},
        json=reopen_payload,
    )
    assert reopened.status_code == 200, reopened.text
    reopened_result = reopened.json()

    reprepared = _prepare(client, headers, 2026, 7).json()
    reclosed = _confirm(client, headers, reprepared, 2026, 7)
    assert reclosed.status_code == 200, reclosed.text
    assert reclosed.json()["current_revision_id"] != confirmed_result["current_revision_id"]

    confirm_retry = _confirm(client, headers, prepared, 2026, 7, key=confirm_key)
    reopen_retry = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**headers, "X-Idempotency-Key": reopen_key},
        json=reopen_payload,
    )
    assert confirm_retry.status_code == reopen_retry.status_code == 200
    assert confirm_retry.json() == confirmed_result
    assert reopen_retry.json() == reopened_result


def test_idempotency_collision_precedes_missing_target_period_lookup(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Month Close Idempotency Target Period")
    prepared = _prepare(client, headers, 2026, 7).json()
    confirm_key = f"confirm-target-period-{uuid.uuid4()}"
    confirmed = _confirm(client, headers, prepared, 2026, 7, key=confirm_key)
    assert confirmed.status_code == 200, confirmed.text

    for endpoint, payload in (
        (
            "/api/v1/month-close/2026/6/confirm",
            {"version": 1, "confirm": True, "prepare_token": "0" * 64},
        ),
        (
            "/api/v1/month-close/2026/6/reopen",
            {"version": 1, "reason": "Different action and period"},
        ),
    ):
        response = client.post(
            endpoint,
            headers={**headers, "X-Idempotency-Key": confirm_key},
            json=payload,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MONTH_CLOSE_IDEMPOTENCY_CONFLICT"

    reopen_key = f"reopen-target-period-{uuid.uuid4()}"
    reopened = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**headers, "X-Idempotency-Key": reopen_key},
        json={"version": confirmed.json()["version"], "reason": "Create durable reopen key"},
    )
    assert reopened.status_code == 200, reopened.text

    for endpoint, payload in (
        (
            "/api/v1/month-close/2026/6/reopen",
            {"version": 1, "reason": "Different period"},
        ),
        (
            "/api/v1/month-close/2026/6/confirm",
            {"version": 1, "confirm": True, "prepare_token": "0" * 64},
        ),
    ):
        response = client.post(
            endpoint,
            headers={**headers, "X-Idempotency-Key": reopen_key},
            json=payload,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MONTH_CLOSE_IDEMPOTENCY_CONFLICT"


def test_fingerprint_is_canonical_financial_and_ignores_cosmetic_metadata(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Month Close Fingerprint")
    account = _account(client, headers)
    category = _category(client, headers, "income")
    transaction = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        category_id=str(category["id"]),
    )
    assert transaction.status_code == 201
    first = _prepare(client, headers, 2026, 7).json()
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    assert canonical_decimal(Decimal("1.2")) == "1.2000"

    renamed_account = client.patch(
        f"/api/v1/accounts/{account['id']}",
        headers=headers,
        json={"version": account["version"], "name": "Cosmetic account rename"},
    )
    renamed_category = client.patch(
        f"/api/v1/categories/{category['id']}",
        headers=headers,
        json={"version": category["version"], "name": "Cosmetic category rename"},
    )
    assert renamed_account.status_code == renamed_category.status_code == 200
    second = _prepare(client, headers, 2026, 7).json()
    assert second["prepared_fingerprint"] == first["prepared_fingerprint"]
    # Labels are part of the user-confirmed report snapshot, so the preview
    # token changes even though the immutable financial state does not.
    assert second["prepare_token"] != first["prepare_token"]

    async def compare_statuses_and_amount() -> tuple[str, str, str]:
        async with AsyncSessionFactory() as session:
            workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
            workspace = await session.get(Workspace, workspace_id)
            row = await session.get(FinancialTransaction, uuid.UUID(transaction.json()["id"]))
            assert workspace is not None and row is not None
            _, cutoff = month_close_service.period_bounds(date(2026, 7, 1), workspace.timezone)
            confirmed_hash = await financial_fingerprint(session, workspace_id, cutoff)
            row.status = "reconciled"
            await session.flush()
            reconciled_hash = await financial_fingerprint(session, workspace_id, cutoff)
            row.amount = Decimal("11.0000")
            await session.flush()
            changed_hash = await financial_fingerprint(session, workspace_id, cutoff)
            await session.rollback()
            return confirmed_hash, reconciled_hash, changed_hash

    confirmed_hash, reconciled_hash, changed_hash = asyncio.run(compare_statuses_and_amount())
    assert confirmed_hash == reconciled_hash
    assert changed_hash != confirmed_hash


def test_reconciliation_after_close_is_allowed_and_fingerprint_equivalent(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Month Close Reconciliation")
    account = _account(client, headers)
    category = _category(client, headers, "income")
    transaction = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        category_id=str(category["id"]),
        amount="10.0000",
    )
    assert transaction.status_code == 201
    closed = _close(client, headers)

    preview = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/preview",
        headers=headers,
        json={
            "statement_date": "2026-07-31",
            "statement_balance": "110.0000",
            "currency": "RUB",
            "account_version": account["version"],
        },
    )
    assert preview.status_code == 200, preview.text
    confirmation = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json={
            "statement_date": preview.json()["statement_date"],
            "statement_balance": preview.json()["statement_balance"],
            "currency": preview.json()["currency"],
            "account_version": account["version"],
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": f"reconcile-after-close-{uuid.uuid4()}",
        },
    )
    assert confirmation.status_code == 200, confirmation.text

    async def inspect() -> tuple[str, str, str]:
        async with AsyncSessionFactory() as session:
            revision = await session.get(
                MonthCloseRevision, uuid.UUID(str(closed["current_revision_id"]))
            )
            row = await session.get(FinancialTransaction, uuid.UUID(transaction.json()["id"]))
            workspace = await session.get(Workspace, uuid.UUID(str(identity["workspace"]["id"])))
            assert revision is not None and row is not None and workspace is not None
            _, cutoff = month_close_service.period_bounds(date(2026, 7, 1), workspace.timezone)
            return (
                row.status,
                str(revision.financial_fingerprint),
                await financial_fingerprint(session, workspace.id, cutoff),
            )

    status, closed_fingerprint, current_fingerprint = asyncio.run(inspect())
    assert status == "reconciled"
    assert current_fingerprint == closed_fingerprint
    assert client.get("/api/v1/month-close/2026/7", headers=headers).json()["status"] == "confirmed"


async def _context(
    session: object,
    user_id: str,
    workspace_id: str,
    *,
    role: str = "owner",
) -> RequestContext:
    user = await session.get(User, uuid.UUID(user_id))  # type: ignore[attr-defined]
    workspace = await session.get(Workspace, uuid.UUID(workspace_id))  # type: ignore[attr-defined]
    assert user is not None and workspace is not None
    return RequestContext(
        user=user,
        workspace=workspace,
        role=role,
        request_id=str(uuid.uuid4()),
    )


def test_two_concurrent_first_prepare_share_one_closure(client: TestClient) -> None:
    identity, _ = _register(client, "Month Close Prepare Race")
    user_id = str(identity["user"]["id"])
    workspace_id = str(identity["workspace"]["id"])

    async def race() -> list[object]:
        start = asyncio.Event()

        async def run_prepare() -> object:
            async with AsyncSessionFactory() as session:
                context = await _context(session, user_id, workspace_id)
                await start.wait()
                try:
                    return await month_close_service.prepare(
                        session,
                        context.workspace,
                        date(2026, 7, 1),
                        actor_user_id=context.user.id,
                        request_id=context.request_id,
                        source="api",
                    )
                except Exception as exc:  # pragma: no cover - assertion reports exact failure
                    return exc

        tasks = [asyncio.create_task(run_prepare()) for _ in range(2)]
        start.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(race())
    assert all(isinstance(item, MonthClosure) for item in results), results
    counts = asyncio.run(_database_counts(workspace_id))
    assert counts == {"closures": 1, "revisions": 0}


def test_concurrent_confirm_is_idempotent_and_creates_one_revision(client: TestClient) -> None:
    identity, headers = _register(client, "Month Close Confirm Race")
    prepared = _prepare(client, headers, 2026, 7).json()
    user_id = str(identity["user"]["id"])
    workspace_id = str(identity["workspace"]["id"])
    key = f"confirm-race-{uuid.uuid4()}"

    async def race() -> list[object]:
        start = asyncio.Event()

        async def run_confirm() -> object:
            async with AsyncSessionFactory() as session:
                context = await _context(session, user_id, workspace_id)
                await start.wait()
                try:
                    return await month_close_service.confirm(
                        session,
                        context,
                        date(2026, 7, 1),
                        version=int(prepared["version"]),
                        explicit=True,
                        prepare_token=str(prepared["prepare_token"]),
                        idempotency_key=key,
                    )
                except Exception as exc:  # pragma: no cover - assertion reports exact failure
                    return exc

        tasks = [asyncio.create_task(run_confirm()) for _ in range(2)]
        start.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(race())
    assert all(isinstance(item, MonthClosure) for item in results), results
    assert asyncio.run(_database_counts(workspace_id))["revisions"] == 1


@pytest.mark.parametrize("mutation_kind", ["create", "update"])
def test_confirm_serializes_with_financial_mutation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    mutation_kind: str,
) -> None:
    identity, headers = _register(client, f"Month Close Mutation Race {mutation_kind}")
    account = _account(client, headers)
    category = _category(client, headers, "income")
    existing = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        category_id=str(category["id"]),
    )
    assert existing.status_code == 201
    prepared = _prepare(client, headers, 2026, 7).json()
    user_id = str(identity["user"]["id"])
    workspace_id = str(identity["workspace"]["id"])
    original_collect = month_close_service.collect_preview

    async def race() -> tuple[object, object, bool]:
        confirmation_locked = asyncio.Event()
        release_confirmation = asyncio.Event()
        mutation_finished = asyncio.Event()

        async def paused_collect(*args: object, **kwargs: object):
            confirmation_locked.set()
            await release_confirmation.wait()
            return await original_collect(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(month_close_service, "collect_preview", paused_collect)

        async def confirm() -> object:
            async with AsyncSessionFactory() as session:
                context = await _context(session, user_id, workspace_id)
                return await month_close_service.confirm(
                    session,
                    context,
                    date(2026, 7, 1),
                    version=int(prepared["version"]),
                    explicit=True,
                    prepare_token=str(prepared["prepare_token"]),
                    idempotency_key=f"confirm-mutation-race-{uuid.uuid4()}",
                )

        async def mutate() -> object:
            async with AsyncSessionFactory() as session:
                context = await _context(session, user_id, workspace_id)
                try:
                    if mutation_kind == "create":
                        result = await transaction_service.create_transaction(
                            session,
                            context,
                            TransactionCreate(
                                occurred_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
                                transaction_type="income",
                                amount=Decimal("5.0000"),
                                currency="RUB",
                                account_id=uuid.UUID(str(account["id"])),
                                category_id=uuid.UUID(str(category["id"])),
                                status="confirmed",
                                source="manual",
                            ),
                        )
                    else:
                        result = await transaction_service.update_transaction(
                            session,
                            context,
                            uuid.UUID(existing.json()["id"]),
                            TransactionUpdate(
                                version=int(existing.json()["version"]),
                                amount=Decimal("12.0000"),
                            ),
                        )
                    mutation_finished.set()
                    return result
                except ApiError as exc:
                    mutation_finished.set()
                    return exc

        confirm_task = asyncio.create_task(confirm())
        await asyncio.wait_for(confirmation_locked.wait(), timeout=5)
        mutation_task = asyncio.create_task(mutate())
        try:
            await asyncio.wait_for(asyncio.shield(mutation_finished.wait()), timeout=0.5)
            completed_before_close = True
        except TimeoutError:
            completed_before_close = False
        release_confirmation.set()
        confirmation = await asyncio.wait_for(confirm_task, timeout=5)
        mutation = await asyncio.wait_for(mutation_task, timeout=5)
        return confirmation, mutation, completed_before_close

    confirmation, mutation, completed_before_close = asyncio.run(race())
    assert isinstance(confirmation, MonthClosure)
    assert completed_before_close is False
    assert isinstance(mutation, ApiError)
    assert mutation.code == "MONTH_CLOSED"


def test_financial_mutation_first_makes_waiting_confirm_stale(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Month Close Reverse Mutation Race")
    account = _account(client, headers)
    category = _category(client, headers, "income")
    prepared = _prepare(client, headers, 2026, 7).json()
    user_id = str(identity["user"]["id"])
    workspace_id = str(identity["workspace"]["id"])
    original_audit = transaction_service.record_audit

    async def race() -> tuple[object, object, bool]:
        mutation_locked = asyncio.Event()
        release_mutation = asyncio.Event()

        async def paused_audit(*args: object, **kwargs: object):
            if kwargs.get("entity_type") == "transaction" and kwargs.get("action") == "create":
                mutation_locked.set()
                await release_mutation.wait()
            return await original_audit(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(transaction_service, "record_audit", paused_audit)

        async def mutate() -> object:
            async with AsyncSessionFactory() as session:
                context = await _context(session, user_id, workspace_id)
                return await transaction_service.create_transaction(
                    session,
                    context,
                    TransactionCreate(
                        occurred_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
                        transaction_type="income",
                        amount=Decimal("5.0000"),
                        currency="RUB",
                        account_id=uuid.UUID(str(account["id"])),
                        category_id=uuid.UUID(str(category["id"])),
                        status="confirmed",
                        source="manual",
                    ),
                )

        async def confirm() -> object:
            async with AsyncSessionFactory() as session:
                context = await _context(session, user_id, workspace_id)
                try:
                    return await month_close_service.confirm(
                        session,
                        context,
                        date(2026, 7, 1),
                        version=int(prepared["version"]),
                        explicit=True,
                        prepare_token=str(prepared["prepare_token"]),
                        idempotency_key=f"reverse-mutation-race-{uuid.uuid4()}",
                    )
                except ApiError as exc:
                    return exc

        mutation_task = asyncio.create_task(mutate())
        await asyncio.wait_for(mutation_locked.wait(), timeout=5)
        confirm_task = asyncio.create_task(confirm())
        try:
            await asyncio.wait_for(asyncio.shield(confirm_task), timeout=0.5)
            completed_before_mutation = True
        except TimeoutError:
            completed_before_mutation = False
        release_mutation.set()
        mutation = await asyncio.wait_for(mutation_task, timeout=5)
        confirmation = await asyncio.wait_for(confirm_task, timeout=5)
        return mutation, confirmation, completed_before_mutation

    mutation, confirmation, completed_before_mutation = asyncio.run(race())
    assert isinstance(mutation, FinancialTransaction)
    assert completed_before_mutation is False
    assert isinstance(confirmation, ApiError)
    assert confirmation.code == "MONTH_CLOSE_PREVIEW_STALE"
    assert asyncio.run(_database_counts(workspace_id))["revisions"] == 0


def test_confirm_and_bulk_import_serialize_in_both_orders(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "import_storage_path",
        Path(f"/tmp/finspace-month-close-race-{uuid.uuid4()}"),
    )

    async def run_confirm(
        user_id: str,
        workspace_id: str,
        prepared: dict[str, object],
        key_prefix: str,
    ) -> object:
        async with AsyncSessionFactory() as session:
            context = await _context(session, user_id, workspace_id)
            try:
                return await month_close_service.confirm(
                    session,
                    context,
                    date(2026, 7, 1),
                    version=int(prepared["version"]),
                    explicit=True,
                    prepare_token=str(prepared["prepare_token"]),
                    idempotency_key=f"{key_prefix}-{uuid.uuid4()}",
                )
            except ApiError as exc:
                return exc

    async def run_import(
        user_id: str,
        workspace_id: str,
        batch_id: str,
    ) -> object:
        async with AsyncSessionFactory() as session:
            context = await _context(session, user_id, workspace_id)
            try:
                return await import_service.commit_import(
                    session,
                    context,
                    uuid.UUID(batch_id),
                    confirmation=True,
                    idempotency_key=f"bulk-race-{uuid.uuid4()}",
                )
            except ApiError as exc:
                return exc

    # Confirm owns the control row first: the whole import waits and is then
    # rejected before its first ledger/audit/outbox write.
    first_identity, first_headers = _register(client, "Month Close Import Race Confirm First")
    first_account = _account(client, first_headers, name="Confirm-first account")
    first_category = _category(client, first_headers, "expense", name="Confirm-first category")
    first_batch = _ready_import_batch(
        client,
        first_headers,
        account_name=str(first_account["name"]),
        category_name=str(first_category["name"]),
    )
    first_prepared = _prepare(client, first_headers, 2026, 7).json()
    original_collect = month_close_service.collect_preview

    async def confirm_first() -> tuple[object, object, bool]:
        confirmation_locked = asyncio.Event()
        release_confirmation = asyncio.Event()

        async def paused_collect(*args: object, **kwargs: object):
            confirmation_locked.set()
            await release_confirmation.wait()
            return await original_collect(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(month_close_service, "collect_preview", paused_collect)
        confirm_task = asyncio.create_task(
            run_confirm(
                str(first_identity["user"]["id"]),
                str(first_identity["workspace"]["id"]),
                first_prepared,
                "confirm-first-import-race",
            )
        )
        await asyncio.wait_for(confirmation_locked.wait(), timeout=5)
        import_task = asyncio.create_task(
            run_import(
                str(first_identity["user"]["id"]),
                str(first_identity["workspace"]["id"]),
                first_batch,
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(import_task), timeout=0.5)
            completed_before_close = True
        except TimeoutError:
            completed_before_close = False
        release_confirmation.set()
        confirmation = await asyncio.wait_for(confirm_task, timeout=5)
        imported = await asyncio.wait_for(import_task, timeout=5)
        return confirmation, imported, completed_before_close

    confirmation, rejected_import, import_completed_before_close = asyncio.run(confirm_first())
    assert isinstance(confirmation, MonthClosure)
    assert import_completed_before_close is False
    assert isinstance(rejected_import, ApiError)
    assert rejected_import.code == "MONTH_CLOSED"
    assert client.get(f"/api/v1/imports/{first_batch}", headers=first_headers).json()["status"] == (
        "ready"
    )
    assert client.get("/api/v1/transactions", headers=first_headers).json()["page"]["total"] == 0

    # Import owns the control row first: confirm waits for the atomic two-row
    # commit, recomputes the snapshot, and rejects the stale prepare token.
    second_identity, second_headers = _register(client, "Month Close Import Race Import First")
    second_account = _account(client, second_headers, name="Import-first account")
    second_category = _category(client, second_headers, "expense", name="Import-first category")
    second_batch = _ready_import_batch(
        client,
        second_headers,
        account_name=str(second_account["name"]),
        category_name=str(second_category["name"]),
    )
    second_prepared = _prepare(client, second_headers, 2026, 7).json()
    original_audit = import_service.record_audit

    async def import_first() -> tuple[object, object, bool]:
        import_locked = asyncio.Event()
        release_import = asyncio.Event()

        async def paused_audit(*args: object, **kwargs: object):
            if kwargs.get("entity_type") == "transaction" and kwargs.get("action") == "create":
                import_locked.set()
                await release_import.wait()
            return await original_audit(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(import_service, "record_audit", paused_audit)
        import_task = asyncio.create_task(
            run_import(
                str(second_identity["user"]["id"]),
                str(second_identity["workspace"]["id"]),
                second_batch,
            )
        )
        await asyncio.wait_for(import_locked.wait(), timeout=5)
        confirm_task = asyncio.create_task(
            run_confirm(
                str(second_identity["user"]["id"]),
                str(second_identity["workspace"]["id"]),
                second_prepared,
                "import-first-confirm-race",
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(confirm_task), timeout=0.5)
            completed_before_import = True
        except TimeoutError:
            completed_before_import = False
        release_import.set()
        imported = await asyncio.wait_for(import_task, timeout=5)
        confirmation = await asyncio.wait_for(confirm_task, timeout=5)
        return imported, confirmation, completed_before_import

    imported, stale_confirmation, confirm_completed_before_import = asyncio.run(import_first())
    assert isinstance(imported, tuple)
    assert imported[1] == 2
    assert confirm_completed_before_import is False
    assert isinstance(stale_confirmation, ApiError)
    assert stale_confirmation.code == "MONTH_CLOSE_PREVIEW_STALE"
    assert client.get(f"/api/v1/imports/{second_batch}", headers=second_headers).json()[
        "status"
    ] == ("imported")
    assert client.get("/api/v1/transactions", headers=second_headers).json()["page"]["total"] == 2
    assert asyncio.run(_database_counts(str(second_identity["workspace"]["id"])))["revisions"] == 0


def test_stale_prepare_and_version_leave_close_state_atomic(client: TestClient) -> None:
    identity, headers = _register(client, "Month Close Stale")
    account = _account(client, headers)
    category = _category(client, headers, "income")
    prepared = _prepare(client, headers, 2026, 7).json()

    stale_version = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": f"stale-version-{uuid.uuid4()}"},
        json={
            "version": int(prepared["version"]) - 1,
            "confirm": True,
            "prepare_token": prepared["prepare_token"],
        },
    )
    assert stale_version.status_code == 409
    assert stale_version.json()["error"]["code"] == "MONTH_CLOSE_VERSION_CONFLICT"

    changed = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        category_id=str(category["id"]),
    )
    assert changed.status_code == 201
    stale_preview = _confirm(client, headers, prepared, 2026, 7)
    assert stale_preview.status_code == 409
    assert stale_preview.json()["error"]["code"] == "MONTH_CLOSE_PREVIEW_STALE"

    async def inspect() -> tuple[str, date | None, int, int]:
        async with AsyncSessionFactory() as session:
            closure = await session.scalar(
                select(MonthClosure).where(
                    MonthClosure.workspace_id == uuid.UUID(str(identity["workspace"]["id"])),
                    MonthClosure.period_month == date(2026, 7, 1),
                )
            )
            control = await session.get(
                MonthCloseControl, uuid.UUID(str(identity["workspace"]["id"]))
            )
            assert closure is not None and control is not None
            revisions = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MonthCloseRevision)
                    .where(MonthCloseRevision.workspace_id == control.workspace_id)
                )
                or 0
            )
            confirms = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.workspace_id == control.workspace_id,
                        AuditLog.action == "month_close.confirm",
                    )
                )
                or 0
            )
            return closure.status, control.closed_through, revisions, confirms

    assert asyncio.run(inspect()) == ("ready", None, 0, 0)


def test_cross_workspace_closed_cutoff_is_isolated(client: TestClient) -> None:
    _, first_headers = _register(client, "Month Close Workspace A")
    _, second_headers = _register(client, "Month Close Workspace B")
    first_account = _account(client, first_headers)
    second_account = _account(client, second_headers)
    first_category = _category(client, first_headers, "income")
    second_category = _category(client, second_headers, "income")
    _close(client, first_headers)

    blocked = _transaction(
        client,
        first_headers,
        account_id=str(first_account["id"]),
        category_id=str(first_category["id"]),
    )
    allowed = _transaction(
        client,
        second_headers,
        account_id=str(second_account["id"]),
        category_id=str(second_category["id"]),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "MONTH_CLOSED"
    assert allowed.status_code == 201, allowed.text


def test_multi_currency_refund_adjustment_report_parity_and_dst_cutoff(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Month Close Report Parity")

    async def set_timezone() -> None:
        async with AsyncSessionFactory() as session:
            workspace = await session.get(Workspace, uuid.UUID(str(identity["workspace"]["id"])))
            assert workspace is not None
            workspace.timezone = "America/New_York"
            await session.commit()

    asyncio.run(set_timezone())
    rub = _account(client, headers, name="RUB", opening_balance="100")
    rub_target = _account(client, headers, name="RUB target", opening_balance="0")
    usd = _account(client, headers, name="USD", currency="USD", opening_balance="50")
    income = _category(client, headers, "income")
    expense = _category(client, headers, "expense")

    income_row = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        category_id=str(income["id"]),
        occurred_at="2026-03-10T12:00:00Z",
        amount="100",
    ).json()
    expense_row = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        category_id=str(expense["id"]),
        occurred_at="2026-03-11T12:00:00Z",
        transaction_type="expense",
        amount="40",
    ).json()
    assert (
        _transaction(
            client,
            headers,
            account_id=str(rub["id"]),
            occurred_at="2026-03-12T12:00:00Z",
            transaction_type="refund",
            related_transaction_id=str(expense_row["id"]),
            amount="10",
        ).status_code
        == 201
    )
    assert (
        _transaction(
            client,
            headers,
            account_id=str(rub["id"]),
            occurred_at="2026-03-13T12:00:00Z",
            transaction_type="adjustment",
            amount="5",
            comment="Audited adjustment",
        ).status_code
        == 201
    )
    assert (
        _transaction(
            client,
            headers,
            account_id=str(rub["id"]),
            target_account_id=str(rub_target["id"]),
            occurred_at="2026-03-14T12:00:00Z",
            transaction_type="transfer",
            amount="20",
        ).status_code
        == 201
    )
    assert (
        _transaction(
            client,
            headers,
            account_id=str(usd["id"]),
            category_id=str(expense["id"]),
            occurred_at="2026-04-01T03:59:59Z",
            transaction_type="expense",
            amount="7.5",
            currency="USD",
        ).status_code
        == 201
    )
    at_cutoff = _transaction(
        client,
        headers,
        account_id=str(rub["id"]),
        category_id=str(income["id"]),
        occurred_at="2026-04-01T04:00:00Z",
        amount="999",
    )
    assert at_cutoff.status_code == 201

    prepared = _prepare(client, headers, 2026, 3)
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["status"] == "ready"
    groups = {item["currency"]: item for item in prepared.json()["summary"]["currencies"]}
    assert set(groups) == {"RUB", "USD"}
    assert groups["RUB"]["income"] == "100.0000"
    assert groups["RUB"]["expense"] == "30.0000"
    assert groups["RUB"]["net_cashflow"] == "75.0000"
    assert groups["RUB"]["transfer_volume"] == "20.0000"
    assert groups["USD"]["expense"] == "7.5000"

    report = client.get(
        "/api/v1/reports/financial",
        headers=headers,
        params={"date_from": "2026-03-01", "date_to": "2026-03-31"},
    )
    assert report.status_code == 200, report.text
    assert prepared.json()["summary"]["currencies"] == report.json()["groups"]
    confirmed = _confirm(client, headers, prepared.json(), 2026, 3)
    assert confirmed.status_code == 200, confirmed.text
    assert income_row["id"]


def test_confirm_failure_rolls_back_closure_control_revision_and_audit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Month Close Atomic Failure")
    prepared = _prepare(client, headers, 2026, 7).json()
    original_record_audit = month_close_service.record_audit

    async def fail_confirm_audit(*args: object, **kwargs: object) -> object:
        if kwargs.get("action") == "month_close.confirm":
            raise RuntimeError("injected month close failure")
        return await original_record_audit(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(month_close_service, "record_audit", fail_confirm_audit)

    async def attempt() -> None:
        async with AsyncSessionFactory() as session:
            context = await _context(
                session,
                str(identity["user"]["id"]),
                str(identity["workspace"]["id"]),
            )
            with pytest.raises(RuntimeError, match="injected month close failure"):
                await month_close_service.confirm(
                    session,
                    context,
                    date(2026, 7, 1),
                    version=int(prepared["version"]),
                    explicit=True,
                    prepare_token=str(prepared["prepare_token"]),
                    idempotency_key=f"atomic-failure-{uuid.uuid4()}",
                )
            await session.rollback()

    asyncio.run(attempt())

    async def inspect() -> tuple[str, date | None, int, int]:
        async with AsyncSessionFactory() as session:
            workspace_id = uuid.UUID(str(identity["workspace"]["id"]))
            closure = await session.scalar(
                select(MonthClosure).where(
                    MonthClosure.workspace_id == workspace_id,
                    MonthClosure.period_month == date(2026, 7, 1),
                )
            )
            control = await session.get(MonthCloseControl, workspace_id)
            assert closure is not None and control is not None
            revision_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MonthCloseRevision)
                    .where(MonthCloseRevision.workspace_id == workspace_id)
                )
                or 0
            )
            confirm_audits = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.workspace_id == workspace_id,
                        AuditLog.action == "month_close.confirm",
                    )
                )
                or 0
            )
            return closure.status, control.closed_through, revision_count, confirm_audits

    assert asyncio.run(inspect()) == ("ready", None, 0, 0)
