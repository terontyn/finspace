import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models.accounts import Account
from app.db.models.google_sync import (
    GoogleConnection,
    GoogleOAuthFlow,
    GoogleSheetBinding,
    SyncConflict,
    SyncInbox,
    SyncOutbox,
    SyncRun,
)
from app.db.models.users import WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.dependencies.google import get_google_client
from app.integrations.google_client import GoogleApiError
from app.main import app
from app.workers.sync_worker import _finish_group, claim_batch
from tests.google_fakes import FakeGoogleClient

PASSWORD = "correct horse battery staple"


def _configure_google(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "google_client_id", SecretStr("fake-client"))
    monkeypatch.setattr(settings, "google_client_secret", SecretStr("fake-secret"))
    monkeypatch.setattr(
        settings,
        "google_token_encryption_key",
        SecretStr(base64.urlsafe_b64encode(b"g" * 32).decode()),
    )
    monkeypatch.setattr(settings, "google_sheets_sync_enabled", True)
    monkeypatch.setattr(settings, "google_oauth_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)


def _register(client: TestClient) -> tuple[dict[str, object], dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"google-{uuid.uuid4()}@example.com",
            "display_name": "Google Owner",
            "password": PASSWORD,
            "workspace_name": "Google Workspace",
            "base_currency": "RUB",
            "timezone": "Asia/Yekaterinburg",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload, {
        "Authorization": f"Bearer {payload['access_token']}",
        "X-Workspace-ID": payload["workspace"]["id"],
    }


def _state(connect_response: dict[str, str]) -> str:
    return parse_qs(urlparse(connect_response["authorization_url"]).query)["state"][0]


def _connect_google(
    client: TestClient,
    headers: dict[str, str],
    fake: FakeGoogleClient,
) -> str:
    app.dependency_overrides[get_google_client] = lambda: fake
    connect = client.post("/api/v1/integrations/google/connect", headers=headers)
    assert connect.status_code == 200, connect.text
    state = _state(connect.json())
    callback = client.get(
        "/api/v1/integrations/google/callback",
        params={"state": state, "code": "fake-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    return state


def _create_sheet(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    response = client.post("/api/v1/google-sheets/create", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_oauth_state_is_single_use_and_tokens_are_encrypted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    identity, headers = _register(client)
    fake = FakeGoogleClient()
    state = _connect_google(client, headers, fake)

    repeated = client.get(
        "/api/v1/integrations/google/callback",
        params={"state": state, "code": "again"},
        follow_redirects=False,
    )
    assert repeated.status_code == 400
    assert repeated.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"
    status = client.get("/api/v1/integrations/google/status", headers=headers)
    assert status.status_code == 200
    assert "access_token" not in status.text
    assert status.json()["google_email"] == "google@example.com"

    async def inspect() -> tuple[bytes | None, bytes | None, uuid.UUID]:
        async with AsyncSessionFactory() as session:
            connection = await session.scalar(
                select(GoogleConnection).where(
                    GoogleConnection.workspace_id == uuid.UUID(identity["workspace"]["id"])
                )
            )
            assert connection is not None
            return (
                connection.access_token_encrypted,
                connection.refresh_token_encrypted,
                connection.id,
            )

    access, refresh, _ = asyncio.run(inspect())
    assert access and b"fake-access-token" not in access
    assert refresh and b"fake-refresh-token" not in refresh
    app.dependency_overrides.clear()


def test_expired_state_and_missing_scope_are_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    _, headers = _register(client)
    fake = FakeGoogleClient()
    app.dependency_overrides[get_google_client] = lambda: fake
    connect = client.post("/api/v1/integrations/google/connect", headers=headers)
    state = _state(connect.json())

    async def expire() -> None:
        async with AsyncSessionFactory() as session:
            flow = await session.scalar(
                select(GoogleOAuthFlow).where(
                    GoogleOAuthFlow.state_hash == hashlib.sha256(state.encode()).hexdigest()
                )
            )
            assert flow is not None
            flow.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire())
    expired = client.get(
        "/api/v1/integrations/google/callback",
        params={"state": state, "code": "fake"},
        follow_redirects=False,
    )
    assert expired.status_code == 400

    connect = client.post("/api/v1/integrations/google/connect", headers=headers)
    fake.scopes = ["openid"]
    denied = client.get(
        "/api/v1/integrations/google/callback",
        params={"state": _state(connect.json()), "code": "fake"},
        follow_redirects=False,
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "GOOGLE_PERMISSION_DENIED"
    app.dependency_overrides.clear()


def test_sheet_template_outbox_disconnect_and_revoke(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    _, headers = _register(client)
    fake = FakeGoogleClient()
    _connect_google(client, headers, fake)
    binding = _create_sheet(client, headers)
    assert binding["sync_mode"] == "push_only"
    assert len(fake.batch_requests) > 10
    assert any("addConditionalFormatRule" in request for request in fake.batch_requests)
    assert any(
        request.get("repeatCell", {})
        .get("cell", {})
        .get("userEnteredFormat", {})
        .get("numberFormat")
        for request in fake.batch_requests
    )
    assert {"Операции", "Счета", "Категории"}.issubset(fake.sheets)

    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Outbox account",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    )
    assert account.status_code == 201

    async def outbox_count() -> int:
        async with AsyncSessionFactory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncOutbox)
                    .where(SyncOutbox.entity_id == uuid.UUID(account.json()["id"]))
                )
                or 0
            )

    assert asyncio.run(outbox_count()) == 1
    assert client.post("/api/v1/integrations/google/disconnect", headers=headers).status_code == 200

    # Reconnect, then revoke remotely and verify the local ciphertext is erased.
    _connect_google(client, headers, fake)
    revoked = client.post("/api/v1/integrations/google/revoke", headers=headers)
    assert revoked.status_code == 200
    assert fake.revoke_calls == 1
    revoked_status = client.get("/api/v1/integrations/google/status", headers=headers)
    assert revoked_status.status_code == 200
    assert revoked_status.json()["connected"] is False
    assert revoked_status.json()["status"] == "revoked"
    app.dependency_overrides.clear()


def test_only_owner_can_create_the_google_sheet(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    identity, headers = _register(client)
    fake = FakeGoogleClient()
    _connect_google(client, headers, fake)

    async def demote() -> None:
        async with AsyncSessionFactory() as session:
            member = await session.get(
                WorkspaceMember,
                (
                    uuid.UUID(str(identity["workspace"]["id"])),
                    uuid.UUID(str(identity["user"]["id"])),
                ),
            )
            assert member is not None
            member.role = "viewer"
            await session.commit()

    asyncio.run(demote())
    forbidden = client.post("/api/v1/google-sheets/create", headers=headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    app.dependency_overrides.clear()


def _signed_headers(
    binding_id: str,
    secret: str,
    body: bytes,
    nonce: str,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    timestamp_value = str(timestamp if timestamp is not None else int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    key = hashlib.sha256(secret.encode()).digest()
    signature = hmac.new(
        key,
        f"{timestamp_value}\n{nonce}\n{body_hash}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Finspace-Binding-ID": binding_id,
        "X-Finspace-Timestamp": timestamp_value,
        "X-Finspace-Nonce": nonce,
        "X-Finspace-Signature": signature,
    }


def test_webhook_hmac_idempotency_transaction_and_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    identity, headers = _register(client)
    fake = FakeGoogleClient()
    _connect_google(client, headers, fake)
    binding = _create_sheet(client, headers)
    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Sheet account",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    ).json()
    category = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": "Sheet expense", "category_type": "expense"},
    ).json()
    secret = client.post("/api/v1/google-sheets/apps-script/secret", headers=headers).json()
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "spreadsheet_id": binding["spreadsheet_id"],
        "sheet_name": "Операции",
        "row_number": 2,
        "entity_type": "transaction",
        "entity_id": None,
        "expected_version": None,
        "row_hash": None,
        "changed_fields": {
            "occurred_at": "2026-07-22T10:00:00+05:00",
            "transaction_type": "Расход",
            "amount": "125,50",
            "currency": "RUB",
            "description": "Продукты",
            "status": "Подтверждена",
        },
        "visible_row": {
            "_account_id": account["id"],
            "_category_id": category["id"],
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    invalid_headers = _signed_headers(binding["id"], secret["secret"], body, "nonce-invalid")
    invalid_headers["X-Finspace-Signature"] = "0" * 64
    invalid = client.post(
        "/api/v1/google-sheets/webhook/changes", content=body, headers=invalid_headers
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "GOOGLE_WEBHOOK_SIGNATURE_INVALID"
    expired = client.post(
        "/api/v1/google-sheets/webhook/changes",
        content=body,
        headers=_signed_headers(
            binding["id"],
            secret["secret"],
            body,
            "nonce-expired",
            timestamp=int(time.time()) - 1000,
        ),
    )
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "GOOGLE_WEBHOOK_EXPIRED"

    def send_case(candidate: dict[str, object], nonce: str):
        candidate_body = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode()
        return client.post(
            "/api/v1/google-sheets/webhook/changes",
            content=candidate_body,
            headers=_signed_headers(binding["id"], secret["secret"], candidate_body, nonce),
        )

    mismatched = json.loads(json.dumps(payload))
    mismatched["event_id"] = str(uuid.uuid4())
    mismatched["spreadsheet_id"] = "another-workspace-sheet"
    mismatch_result = send_case(mismatched, "nonce-mismatch")
    assert mismatch_result.status_code == 403
    assert mismatch_result.json()["error"]["code"] == "WORKSPACE_ACCESS_DENIED"

    unknown_account = json.loads(json.dumps(payload))
    unknown_account["event_id"] = str(uuid.uuid4())
    unknown_account["visible_row"]["_account_id"] = str(uuid.uuid4())
    unknown_account_result = send_case(unknown_account, "nonce-unknown-account")
    assert unknown_account_result.status_code == 422
    assert unknown_account_result.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"

    unknown_category = json.loads(json.dumps(payload))
    unknown_category["event_id"] = str(uuid.uuid4())
    unknown_category["visible_row"]["_category_id"] = str(uuid.uuid4())
    unknown_category_result = send_case(unknown_category, "nonce-unknown-category")
    assert unknown_category_result.status_code == 422
    assert unknown_category_result.json()["error"]["code"] == "CATEGORY_NOT_FOUND"

    invalid_decimal = json.loads(json.dumps(payload))
    invalid_decimal["event_id"] = str(uuid.uuid4())
    invalid_decimal["changed_fields"]["amount"] = "not-a-decimal"
    invalid_decimal_result = send_case(invalid_decimal, "nonce-invalid-decimal")
    assert invalid_decimal_result.status_code == 422
    assert invalid_decimal_result.json()["error"]["code"] == "VALIDATION_ERROR"

    invalid_transfer = json.loads(json.dumps(payload))
    invalid_transfer["event_id"] = str(uuid.uuid4())
    invalid_transfer["changed_fields"]["transaction_type"] = "Перевод"
    invalid_transfer["visible_row"].pop("_category_id")
    invalid_transfer_result = send_case(invalid_transfer, "nonce-invalid-transfer")
    assert invalid_transfer_result.status_code == 422
    assert invalid_transfer_result.json()["error"]["code"] == "INVALID_TRANSFER"
    assert client.get("/api/v1/transactions", headers=headers).json()["page"]["total"] == 0

    first_headers = _signed_headers(binding["id"], secret["secret"], body, "nonce-first")
    first = client.post(
        "/api/v1/google-sheets/webhook/changes", content=body, headers=first_headers
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "applied"
    transaction_id = first.json()["entity_id"]

    replay = client.post(
        "/api/v1/google-sheets/webhook/changes", content=body, headers=first_headers
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "GOOGLE_WEBHOOK_REPLAY_DETECTED"
    duplicate = client.post(
        "/api/v1/google-sheets/webhook/changes",
        content=body,
        headers=_signed_headers(binding["id"], secret["secret"], body, "nonce-second"),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"

    updated = client.patch(
        f"/api/v1/transactions/{transaction_id}",
        headers=headers,
        json={"version": 1, "amount": "130.00"},
    )
    assert updated.status_code == 200
    payload.update(
        {
            "event_id": str(uuid.uuid4()),
            "entity_id": transaction_id,
            "expected_version": 1,
            "changed_fields": {"amount": "140.00"},
        }
    )
    conflict_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    conflict = client.post(
        "/api/v1/google-sheets/webhook/changes",
        content=conflict_body,
        headers=_signed_headers(binding["id"], secret["secret"], conflict_body, "nonce-conflict"),
    )
    assert conflict.status_code == 200
    assert conflict.json()["status"] == "conflict"
    conflict_list = client.get("/api/v1/google-sheets/conflicts", headers=headers)
    assert conflict_list.status_code == 200
    assert any(
        item["id"] == conflict.json()["conflict_id"] for item in conflict_list.json()["items"]
    )

    async def conflict_count() -> int:
        async with AsyncSessionFactory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncConflict)
                    .where(SyncConflict.binding_id == uuid.UUID(binding["id"]))
                )
                or 0
            )

    conflicts_before_retry = asyncio.run(conflict_count())
    retried_conflict = client.post(
        "/api/v1/google-sheets/webhook/changes",
        content=conflict_body,
        headers=_signed_headers(
            binding["id"],
            secret["secret"],
            conflict_body,
            "nonce-conflict-retry",
        ),
    )
    assert retried_conflict.status_code == 200, retried_conflict.text
    assert retried_conflict.json() == conflict.json()
    assert asyncio.run(conflict_count()) == conflicts_before_retry

    changed_after_conflict = client.patch(
        f"/api/v1/transactions/{transaction_id}",
        headers=headers,
        json={"version": 2, "amount": "131.00"},
    )
    assert changed_after_conflict.status_code == 200, changed_after_conflict.text
    stale = client.post(
        f"/api/v1/google-sheets/conflicts/{conflict.json()['conflict_id']}/resolve",
        headers=headers,
        json={"resolution": "keep_database"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "GOOGLE_SYNC_CONFLICT_STALE"

    open_conflicts = client.get(
        "/api/v1/google-sheets/conflicts?status=open",
        headers=headers,
    )
    assert open_conflicts.status_code == 200
    assert any(
        item["id"] == conflict.json()["conflict_id"] for item in open_conflicts.json()["items"]
    )
    other_identity, other_headers = _register(client)
    assert (
        client.get(
            f"/api/v1/google-sheets/conflicts/{conflict.json()['conflict_id']}",
            headers=other_headers,
        ).status_code
        == 404
    )

    async def add_viewer() -> None:
        async with AsyncSessionFactory() as session:
            session.add(
                WorkspaceMember(
                    workspace_id=uuid.UUID(str(identity["workspace"]["id"])),
                    user_id=uuid.UUID(str(other_identity["user"]["id"])),
                    role="viewer",
                )
            )
            await session.commit()

    asyncio.run(add_viewer())
    viewer_headers = {
        **other_headers,
        "X-Workspace-ID": str(identity["workspace"]["id"]),
    }
    assert (
        client.get(
            "/api/v1/google-sheets/conflicts?status=open",
            headers=viewer_headers,
        ).status_code
        == 200
    )

    payload["event_id"] = str(uuid.uuid4())
    payload["expected_version"] = 2
    payload["changed_fields"] = {"amount": "141.00"}
    second_conflict_body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    second_conflict = client.post(
        "/api/v1/google-sheets/webhook/changes",
        content=second_conflict_body,
        headers=_signed_headers(
            binding["id"],
            secret["secret"],
            second_conflict_body,
            "nonce-conflict-second",
        ),
    )
    assert second_conflict.status_code == 200, second_conflict.text
    assert second_conflict.json()["status"] == "conflict"
    denied_resolution = client.post(
        f"/api/v1/google-sheets/conflicts/{second_conflict.json()['conflict_id']}/resolve",
        headers=viewer_headers,
        json={"resolution": "keep_database"},
    )
    assert denied_resolution.status_code == 403
    resolved = client.post(
        f"/api/v1/google-sheets/conflicts/{second_conflict.json()['conflict_id']}/resolve",
        headers=headers,
        json={"resolution": "keep_database"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolution"] == "keep_database"
    repeated_resolution = client.post(
        f"/api/v1/google-sheets/conflicts/{second_conflict.json()['conflict_id']}/resolve",
        headers=headers,
        json={"resolution": "keep_database"},
    )
    assert repeated_resolution.status_code == 409
    resolved_conflicts = client.get(
        "/api/v1/google-sheets/conflicts?status=resolved",
        headers=headers,
    )
    assert any(
        item["id"] == second_conflict.json()["conflict_id"]
        for item in resolved_conflicts.json()["items"]
    )

    payload["event_id"] = str(uuid.uuid4())
    payload["changed_fields"] = {"_version": 99}
    tamper_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    tamper = client.post(
        "/api/v1/google-sheets/webhook/changes",
        content=tamper_body,
        headers=_signed_headers(binding["id"], secret["secret"], tamper_body, "nonce-tamper"),
    )
    assert tamper.status_code == 422
    assert tamper.json()["error"]["code"] == "GOOGLE_SHEET_TEMPLATE_INVALID"

    account_payload = {
        "event_id": str(uuid.uuid4()),
        "spreadsheet_id": binding["spreadsheet_id"],
        "sheet_name": "Счета",
        "row_number": 2,
        "entity_type": "account",
        "entity_id": account["id"],
        "expected_version": 1,
        "row_hash": None,
        "changed_fields": {
            "name": "Sheet account renamed",
            "account_type": "cash",
            "institution": "Local bank",
            "is_archived": "Нет",
        },
        "visible_row": {},
    }
    account_body = json.dumps(account_payload, ensure_ascii=False, separators=(",", ":")).encode()
    account_result = client.post(
        "/api/v1/google-sheets/webhook/changes",
        content=account_body,
        headers=_signed_headers(binding["id"], secret["secret"], account_body, "nonce-account"),
    )
    assert account_result.status_code == 200, account_result.text
    assert account_result.json()["version"] == 2
    assert (
        client.get(f"/api/v1/accounts/{account['id']}", headers=headers).json()["name"]
        == "Sheet account renamed"
    )
    account_payload["event_id"] = str(uuid.uuid4())
    account_payload["expected_version"] = 2
    account_payload["changed_fields"] = {"currency": "USD"}
    forbidden_body = json.dumps(account_payload, ensure_ascii=False, separators=(",", ":")).encode()
    forbidden = client.post(
        "/api/v1/google-sheets/webhook/changes",
        content=forbidden_body,
        headers=_signed_headers(binding["id"], secret["secret"], forbidden_body, "nonce-forbidden"),
    )
    assert forbidden.status_code == 422

    transaction_count = client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]
    invalid_decimal["changed_fields"]["amount"] = "126,50"
    recovered_decimal = send_case(invalid_decimal, "nonce-recovered-decimal")
    assert recovered_decimal.status_code == 200, recovered_decimal.text
    assert recovered_decimal.json()["status"] == "applied"
    assert (
        client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]
        == transaction_count + 1
    )
    duplicate_decimal = send_case(invalid_decimal, "nonce-duplicate-decimal")
    assert duplicate_decimal.status_code == 200
    assert duplicate_decimal.json()["status"] == "duplicate"
    assert (
        client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]
        == transaction_count + 1
    )
    app.dependency_overrides.clear()


def test_outbox_covers_entities_lifecycle_and_paused_binding(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    _, headers = _register(client)
    fake = FakeGoogleClient()
    _connect_google(client, headers, fake)
    _create_sheet(client, headers)

    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": f"Lifecycle account {uuid.uuid4().hex[:8]}",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    ).json()
    category = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": f"Lifecycle category {uuid.uuid4().hex[:8]}", "category_type": "expense"},
    ).json()
    transaction_response = client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": "2026-07-22T10:00:00Z",
            "transaction_type": "expense",
            "amount": "10.00",
            "currency": "RUB",
            "account_id": account["id"],
            "category_id": category["id"],
            "status": "confirmed",
            "source": "manual",
            "splits": [],
        },
    )
    assert transaction_response.status_code == 201, transaction_response.text
    transaction = transaction_response.json()
    updated_category = client.patch(
        f"/api/v1/categories/{category['id']}",
        headers=headers,
        json={"version": category["version"], "color": "#123456"},
    )
    assert updated_category.status_code == 200
    cancelled = client.post(
        f"/api/v1/transactions/{transaction['id']}/cancel",
        headers=headers,
        json={"version": transaction["version"]},
    ).json()
    deleted = client.delete(
        f"/api/v1/transactions/{transaction['id']}?version={cancelled['version']}",
        headers=headers,
    ).json()
    restored = client.post(
        f"/api/v1/transactions/{transaction['id']}/restore",
        headers=headers,
        json={"version": deleted["version"]},
    )
    assert restored.status_code == 200

    duplicate = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": account["name"],
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    )
    assert duplicate.status_code == 409
    assert client.post("/api/v1/google-sheets/pause", headers=headers).status_code == 200
    paused_account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": f"Paused account {uuid.uuid4().hex[:8]}",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    ).json()

    async def inspect() -> tuple[list[str], int, int, int]:
        async with AsyncSessionFactory() as session:
            transaction_events = list(
                (
                    await session.scalars(
                        select(SyncOutbox)
                        .where(SyncOutbox.entity_id == uuid.UUID(transaction["id"]))
                        .order_by(SyncOutbox.entity_version)
                    )
                ).all()
            )
            category_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncOutbox)
                    .where(SyncOutbox.entity_id == uuid.UUID(category["id"]))
                )
                or 0
            )
            account_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncOutbox)
                    .where(SyncOutbox.entity_id == uuid.UUID(account["id"]))
                )
                or 0
            )
            paused_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncOutbox)
                    .where(SyncOutbox.entity_id == uuid.UUID(paused_account["id"]))
                )
                or 0
            )
            return (
                [event.operation for event in transaction_events],
                category_count,
                account_count,
                paused_count,
            )

    operations, category_count, account_count, paused_count = asyncio.run(inspect())
    assert operations == ["upsert", "upsert", "delete", "restore"]
    assert category_count == 2
    assert account_count == 1
    assert paused_count == 1
    app.dependency_overrides.clear()


def test_import_commit_and_rollback_enqueue_outbox(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    monkeypatch.setattr(settings, "import_storage_path", Path("/tmp/finspace-google-imports"))
    _, headers = _register(client)
    fake = FakeGoogleClient()
    _connect_google(client, headers, fake)
    _create_sheet(client, headers)
    account_name = f"Import sync account {uuid.uuid4().hex[:8]}"
    category_name = f"Import sync category {uuid.uuid4().hex[:8]}"
    assert (
        client.post(
            "/api/v1/accounts",
            headers=headers,
            json={
                "name": account_name,
                "account_type": "cash",
                "currency": "RUB",
                "opening_balance": "0",
                "opening_balance_at": "2026-07-22T00:00:00Z",
            },
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/categories",
            headers=headers,
            json={"name": category_name, "category_type": "expense"},
        ).status_code
        == 201
    )
    content = (
        "Дата;Тип;Сумма;Счёт;Категория;Описание\n"
        f"22.07.2026;Расход;42,50;{account_name};{category_name};Google outbox\n"
    ).encode()
    uploaded = client.post(
        "/api/v1/imports",
        headers=headers,
        files={"file": ("google-outbox.csv", content, "text/csv")},
    )
    assert uploaded.status_code == 201, uploaded.text
    batch_id = uploaded.json()["id"]
    mapping = {
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
    assert (
        client.put(f"/api/v1/imports/{batch_id}/mapping", headers=headers, json=mapping).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/imports/{batch_id}/validate", headers=headers).json()["summary"][
            "valid"
        ]
        == 1
    )
    committed = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        headers={**headers, "X-Idempotency-Key": str(uuid.uuid4())},
        json={"confirm": True},
    )
    assert committed.status_code == 200, committed.text
    imported = next(
        item
        for item in client.get("/api/v1/transactions", headers=headers).json()["items"]
        if item["source"] == "import"
    )
    rolled_back = client.post(
        f"/api/v1/imports/{batch_id}/rollback",
        headers=headers,
        json={"force": False},
    )
    assert rolled_back.status_code == 200, rolled_back.text

    async def operations() -> list[str]:
        async with AsyncSessionFactory() as session:
            events = list(
                (
                    await session.scalars(
                        select(SyncOutbox)
                        .where(SyncOutbox.entity_id == uuid.UUID(imported["id"]))
                        .order_by(SyncOutbox.entity_version)
                    )
                ).all()
            )
            return [event.operation for event in events]

    assert asyncio.run(operations()) == ["upsert", "delete"]
    app.dependency_overrides.clear()


def test_reconciliation_creates_run_and_restores_missing_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    _, headers = _register(client)
    fake = FakeGoogleClient()
    _connect_google(client, headers, fake)
    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Before export",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    ).json()
    binding = _create_sheet(client, headers)
    fake.sheets["Счета"] = []  # Simulate a physically deleted row.
    response = client.post("/api/v1/google-sheets/reconcile", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["results"]["missing_in_sheet"] >= 1

    async def inspect() -> tuple[int, int, bool]:
        async with AsyncSessionFactory() as session:
            runs = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncRun)
                    .where(
                        SyncRun.run_type == "reconciliation",
                        SyncRun.binding_id == uuid.UUID(binding["id"]),
                    )
                )
                or 0
            )
            queued = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncOutbox)
                    .where(
                        SyncOutbox.entity_id == uuid.UUID(account["id"]),
                        SyncOutbox.idempotency_key.contains(":reconcile:"),
                    )
                )
                or 0
            )
            db_account = await session.get(Account, uuid.UUID(account["id"]))
            return runs, queued, db_account is not None and db_account.deleted_at is None

    runs, queued, preserved = asyncio.run(inspect())
    assert runs == 1
    assert queued == 1
    assert preserved
    app.dependency_overrides.clear()


def test_reconciliation_classifies_hash_version_duplicates_and_tamper(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    _, headers = _register(client)
    fake = FakeGoogleClient()
    _connect_google(client, headers, fake)
    accounts: list[dict[str, object]] = []
    for index in range(7):
        response = client.post(
            "/api/v1/accounts",
            headers=headers,
            json={
                "name": f"Reconcile {index} {uuid.uuid4().hex[:8]}",
                "account_type": "cash",
                "currency": "RUB",
                "opening_balance": "0",
                "opening_balance_at": "2026-07-22T00:00:00Z",
            },
        )
        assert response.status_code == 201
        accounts.append(response.json())
    _create_sheet(client, headers)
    by_id = {str(row[12]): list(row) for row in fake.sheets["Счета"]}
    rows = [by_id[str(account["id"])] for account in accounts]
    duplicate_a = list(rows[1])
    duplicate_b = list(rows[1])
    database_newer = list(rows[2])
    database_newer[13] = 0
    sheet_newer = list(rows[3])
    sheet_newer[13] = 2
    hash_conflict = list(rows[4])
    hash_conflict[14] = "0" * 64
    technical_tamper = list(rows[5])
    technical_tamper[16] = "2026-07-22T00:00:00Z"
    unknown = list(rows[0])
    unknown[12] = str(uuid.uuid4())
    invalid = list(rows[0])
    invalid[12] = "not-a-uuid"
    fake.sheets["Счета"] = [
        rows[0],
        duplicate_a,
        duplicate_b,
        database_newer,
        sheet_newer,
        hash_conflict,
        technical_tamper,
        unknown,
        invalid,
    ]

    response = client.post("/api/v1/google-sheets/reconcile", headers=headers)
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert results["matched"] >= 1
    assert results["duplicate_in_sheet"] == 2
    assert results["database_newer"] >= 1
    assert results["sheet_newer"] >= 1
    assert results["conflict"] >= 2
    assert results["unknown_in_sheet"] >= 1
    assert results["invalid"] >= 1
    assert results["missing_in_sheet"] >= 2
    assert client.get("/api/v1/accounts", headers=headers).json()["page"]["total"] == 7
    conflict_items = client.get("/api/v1/google-sheets/conflicts", headers=headers).json()["items"]
    sheet_newer_conflict = next(
        item for item in conflict_items if item["entity_id"] == accounts[3]["id"]
    )
    resolved = client.post(
        f"/api/v1/google-sheets/conflicts/{sheet_newer_conflict['id']}/resolve",
        headers=headers,
        json={"resolution": "keep_sheet"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolution"] == "keep_sheet"
    app.dependency_overrides.clear()


def test_stage4_tables_are_migrated() -> None:
    async def inspect() -> tuple[int, int, int, int, int, int, int]:
        async with AsyncSessionFactory() as session:
            models = (
                GoogleConnection,
                GoogleSheetBinding,
                SyncOutbox,
                SyncInbox,
                SyncConflict,
                SyncRun,
                GoogleOAuthFlow,
            )
            values = []
            for model in models:
                values.append(
                    int(await session.scalar(select(func.count()).select_from(model)) or 0)
                )
            return tuple(values)  # type: ignore[return-value]

    assert len(asyncio.run(inspect())) == 7


def test_worker_lock_retry_completion_and_permanent_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_google(monkeypatch)
    _, headers = _register(client)
    fake = FakeGoogleClient()
    _connect_google(client, headers, fake)
    _create_sheet(client, headers)

    async def cancel_old() -> None:
        async with AsyncSessionFactory() as session:
            events = list(
                (
                    await session.scalars(
                        select(SyncOutbox).where(
                            SyncOutbox.status.in_(("pending", "retry", "processing"))
                        )
                    )
                ).all()
            )
            for event in events:
                event.status = "cancelled"
            await session.commit()

    asyncio.run(cancel_old())
    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Worker account",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    ).json()

    async def exercise() -> tuple[str, str, int]:
        async with AsyncSessionFactory() as first_session:
            first = await claim_batch(first_session)
        target = next(event for event in first if str(event.entity_id) == account["id"])
        async with AsyncSessionFactory() as second_session:
            assert await claim_batch(second_session) == []
        async with AsyncSessionFactory() as session:
            binding = await session.get(GoogleSheetBinding, target.binding_id)
            event = await session.get(SyncOutbox, target.id)
            assert binding is not None and event is not None
            await _finish_group(
                session,
                binding,
                [event],
                GoogleApiError(
                    "GOOGLE_API_RATE_LIMITED",
                    "quota",
                    retryable=True,
                ),
            )
            retry_status = event.status
        async with AsyncSessionFactory() as session:
            event = await session.get(SyncOutbox, target.id)
            assert event is not None
            event.available_at = datetime.now(UTC)
            await session.commit()
        async with AsyncSessionFactory() as session:
            claimed = await claim_batch(session)
            assert len(claimed) == 1
        async with AsyncSessionFactory() as session:
            binding = await session.get(GoogleSheetBinding, target.binding_id)
            event = await session.get(SyncOutbox, target.id)
            assert binding is not None and event is not None
            await _finish_group(session, binding, [event], None)
            completed_status = event.status
        async with AsyncSessionFactory() as session:
            assert await claim_batch(session) == []
            event = await session.get(SyncOutbox, target.id)
            binding = await session.get(GoogleSheetBinding, target.binding_id)
            assert event is not None and binding is not None
            event.status = "processing"
            event.locked_by = "test"
            event.attempt_count += 1
            await _finish_group(
                session,
                binding,
                [event],
                GoogleApiError(
                    "GOOGLE_PERMISSION_DENIED",
                    "permission denied",
                    retryable=False,
                ),
            )
            run_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SyncRun)
                    .where(
                        SyncRun.binding_id == binding.id,
                        SyncRun.run_type == "outbox_push",
                    )
                )
                or 0
            )
            return retry_status, completed_status, run_count

    retry_status, completed_status, run_count = asyncio.run(exercise())
    assert retry_status == "retry"
    assert completed_status == "completed"
    assert run_count == 3
    app.dependency_overrides.clear()
