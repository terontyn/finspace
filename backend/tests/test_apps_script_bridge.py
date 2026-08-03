import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.models.google_sync import GoogleSheetBinding, SyncOutbox, SyncRun
from app.db.session import AsyncSessionFactory

PASSWORD = "correct horse battery staple"


def _configure_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "google_sync_provider", "apps_script_bridge")
    monkeypatch.setattr(settings, "google_oauth_enabled", False)
    monkeypatch.setattr(settings, "apps_script_bridge_enabled", True)
    monkeypatch.setattr(settings, "google_sheets_sync_enabled", True)
    monkeypatch.setattr(settings, "public_backend_url", "https://finspace.test.invalid")
    monkeypatch.setattr(settings, "apps_script_pull_batch_size", 100)
    monkeypatch.setattr(settings, "apps_script_heartbeat_ttl_minutes", 15)


def _register_user(client: TestClient) -> tuple[dict[str, object], dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"bridge-{uuid.uuid4()}@example.com",
            "display_name": "Bridge Owner",
            "password": PASSWORD,
            "workspace_name": "Bridge Workspace",
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


def _signed_headers(
    binding_id: str,
    secret: str,
    body: bytes,
    *,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    if nonce is None:
        raise AssertionError("Tests must pass an explicit nonce")
    timestamp_value = str(timestamp if timestamp is not None else int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    signature = hmac.new(
        hashlib.sha256(secret.encode()).digest(),
        f"{timestamp_value}\n{nonce}\n{body_hash}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Finspace-Binding-ID": binding_id,
        "X-Finspace-Timestamp": timestamp_value,
        "X-Finspace-Nonce": nonce,
        "X-Finspace-Body-SHA256": body_hash,
        "X-Finspace-Signature": signature,
    }


def _bridge_post(
    client: TestClient,
    path: str,
    binding_id: str,
    secret: str,
    payload: dict[str, object],
    *,
    nonce: str | None = None,
):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    actual_nonce = nonce or str(uuid.uuid4())
    return client.post(
        f"/api/v1/google-sheets/apps-script/{path}",
        content=body,
        headers=_signed_headers(
            binding_id,
            secret,
            body,
            nonce=actual_nonce,
        ),
    )


def _create_references(client: TestClient, headers: dict[str, str]) -> tuple[dict, dict]:
    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Bridge account",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    )
    category = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": "Bridge expense", "category_type": "expense"},
    )
    assert account.status_code == 201, account.text
    assert category.status_code == 201, category.text
    return account.json(), category.json()


def _create_binding(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    response = client.post(
        "/api/v1/google-sheets/apps-script/binding",
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _register_sheet(
    client: TestClient,
    binding: dict[str, object],
    *,
    spreadsheet_id: str = "bridge-sheet-1",
):
    return _bridge_post(
        client,
        "register",
        str(binding["id"]),
        str(binding["secret"]),
        {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet_url": (f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"),
            "template_version": 1,
            "apps_script_version": 1,
        },
    )


def test_binding_register_pull_ack_lease_pause_and_heartbeat(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_bridge(monkeypatch)
    identity, headers = _register_user(client)
    account, _ = _create_references(client, headers)
    binding = _create_binding(client, headers)
    assert binding["provider"] == "apps_script_bridge"
    assert binding["spreadsheet_id"] is None
    assert binding["secret"]
    assert binding["backend_url"] == "https://finspace.test.invalid"

    public_binding = client.get(
        "/api/v1/google-sheets/apps-script/binding",
        headers=headers,
    )
    assert public_binding.status_code == 200
    assert "secret" not in public_binding.json()

    source_package = client.get(
        "/api/v1/google-sheets/apps-script/package",
        headers=headers,
    )
    assert source_package.status_code == 200
    sources = source_package.json()["files"]
    assert "Template.gs" in sources
    assert "function setupFinspace()" in sources["Template.gs"]
    assert "X-Finspace-Body-SHA256" in sources["SyncClient.gs"]
    assert "Utilities.newBlob(message).getBytes()" in sources["SyncClient.gs"]
    assert "function updateBackendUrl()" in sources["Config.gs"]
    assert "orderPulledEvents_" in sources["SyncClient.gs"]
    assert "writePulledRow_" in sources["SyncClient.gs"]
    assert "SpreadsheetApp.flush()" in sources["SyncClient.gs"]

    async def inspect_secret() -> tuple[str, uuid.UUID | None]:
        async with AsyncSessionFactory() as session:
            stored = await session.get(GoogleSheetBinding, uuid.UUID(str(binding["id"])))
            assert stored is not None
            return stored.binding_secret_hash, stored.google_connection_id

    secret_hash, connection_id = asyncio.run(inspect_secret())
    assert secret_hash == hashlib.sha256(str(binding["secret"]).encode()).hexdigest()
    assert str(binding["secret"]) not in secret_hash
    assert connection_id is None

    registered = _register_sheet(client, binding)
    assert registered.status_code == 200, registered.text
    assert registered.json()["status"] == "registered"
    assert registered.json()["initial_export_events"] == 2

    repeated = _register_sheet(client, binding)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "already_registered"
    other_sheet = _register_sheet(client, binding, spreadsheet_id="bridge-sheet-other")
    assert other_sheet.status_code == 409
    assert other_sheet.json()["error"]["code"] == "APPS_SCRIPT_REBIND_REQUIRED"

    pull_payload: dict[str, object] = {
        "spreadsheet_id": "bridge-sheet-1",
        "limit": 100,
    }
    pulled = _bridge_post(
        client,
        "pull",
        str(binding["id"]),
        str(binding["secret"]),
        pull_payload,
    )
    assert pulled.status_code == 200, pulled.text
    assert len(pulled.json()["events"]) == 2
    event = pulled.json()["events"][0]
    assert event["entity_type"] in {"account", "category"}
    assert event["row"]

    async def inspect_processing() -> tuple[str, int]:
        async with AsyncSessionFactory() as session:
            outbox = await session.get(SyncOutbox, uuid.UUID(event["event_id"]))
            assert outbox is not None
            return outbox.status, outbox.attempt_count

    assert asyncio.run(inspect_processing()) == ("processing", 1)

    not_released = _bridge_post(
        client,
        "pull",
        str(binding["id"]),
        str(binding["secret"]),
        pull_payload,
    )
    assert not_released.status_code == 200
    assert all(item["event_id"] != event["event_id"] for item in not_released.json()["events"])

    async def expire_lease() -> None:
        async with AsyncSessionFactory() as session:
            outbox = await session.get(SyncOutbox, uuid.UUID(event["event_id"]))
            assert outbox is not None
            outbox.locked_at = datetime.now(UTC) - timedelta(minutes=16)
            await session.commit()

    asyncio.run(expire_lease())
    released = _bridge_post(
        client,
        "pull",
        str(binding["id"]),
        str(binding["secret"]),
        pull_payload,
    )
    assert released.status_code == 200
    assert released.json()["events"][0]["event_id"] == event["event_id"]

    ack_payload = {
        "events": [
            {
                "event_id": event["event_id"],
                "status": "applied",
                "row_number": 2,
                "row_hash": event["row_hash"],
            }
        ]
    }
    acked = _bridge_post(
        client,
        "ack",
        str(binding["id"]),
        str(binding["secret"]),
        ack_payload,
    )
    assert acked.status_code == 200, acked.text
    assert acked.json()["applied"] == 1
    duplicate_ack = _bridge_post(
        client,
        "ack",
        str(binding["id"]),
        str(binding["secret"]),
        ack_payload,
    )
    assert duplicate_ack.status_code == 200
    assert duplicate_ack.json()["duplicates"] == 1

    remaining_event = next(
        item for item in pulled.json()["events"] if item["event_id"] != event["event_id"]
    )
    completed_initial = _bridge_post(
        client,
        "ack",
        str(binding["id"]),
        str(binding["secret"]),
        {
            "events": [
                {
                    "event_id": remaining_event["event_id"],
                    "status": "applied",
                    "row_number": 3,
                    "row_hash": remaining_event["row_hash"],
                }
            ]
        },
    )
    assert completed_initial.status_code == 200
    assert completed_initial.json()["initial_export_completed"] is True

    heartbeat = _bridge_post(
        client,
        "heartbeat",
        str(binding["id"]),
        str(binding["secret"]),
        {"spreadsheet_id": "bridge-sheet-1", "apps_script_version": 1},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["status"] == "ok"

    paused = client.post(
        "/api/v1/google-sheets/apps-script/binding/pause",
        headers=headers,
    )
    assert paused.status_code == 200
    paused_account_response = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": f"Paused bridge account {uuid.uuid4().hex[:8]}",
            "account_type": "cash",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-22T00:00:00Z",
        },
    )
    assert paused_account_response.status_code == 201
    paused_account = paused_account_response.json()

    async def inspect_paused_outbox() -> str | None:
        async with AsyncSessionFactory() as session:
            event = await session.scalar(
                select(SyncOutbox).where(SyncOutbox.entity_id == uuid.UUID(paused_account["id"]))
            )
            return event.status if event is not None else None

    assert asyncio.run(inspect_paused_outbox()) == "pending"
    paused_pull = _bridge_post(
        client,
        "pull",
        str(binding["id"]),
        str(binding["secret"]),
        {"spreadsheet_id": "bridge-sheet-1"},
    )
    assert paused_pull.status_code == 200
    assert paused_pull.json()["status"] == "paused"
    assert paused_pull.json()["events"] == []

    resumed = client.post(
        "/api/v1/google-sheets/apps-script/binding/resume",
        headers=headers,
    )
    assert resumed.status_code == 200
    resumed_pull = _bridge_post(
        client,
        "pull",
        str(binding["id"]),
        str(binding["secret"]),
        {"spreadsheet_id": "bridge-sheet-1"},
    )
    assert resumed_pull.status_code == 200
    assert any(item["entity_id"] == paused_account["id"] for item in resumed_pull.json()["events"])

    status = client.get("/api/v1/google-sheets/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["provider"] == "apps_script_bridge"
    assert status.json()["oauth_enabled"] is False
    assert status.json()["heartbeat_healthy"] is True
    assert status.json()["connection"]["configured"] is False

    async def inspect_workspace() -> uuid.UUID:
        async with AsyncSessionFactory() as session:
            stored = await session.get(GoogleSheetBinding, uuid.UUID(str(binding["id"])))
            assert stored is not None
            return stored.workspace_id

    assert str(asyncio.run(inspect_workspace())) == str(identity["workspace"]["id"])
    assert account["id"]


def test_hmac_replay_push_conflict_isolation_and_reconciliation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_bridge(monkeypatch)
    _, headers = _register_user(client)
    account, category = _create_references(client, headers)
    binding = _create_binding(client, headers)
    spreadsheet_id = "bridge-sheet-2"
    assert _register_sheet(client, binding, spreadsheet_id=spreadsheet_id).status_code == 200

    payload = {
        "events": [
            {
                "event_id": str(uuid.uuid4()),
                "spreadsheet_id": spreadsheet_id,
                "sheet_name": "Операции",
                "row_number": 2,
                "entity_type": "transaction",
                "entity_id": None,
                "expected_version": None,
                "row_hash": None,
                "changed_fields": {
                    "occurred_at": "2026-07-22T10:00:00+05:00",
                    "transaction_type": "Расход",
                    "amount": "350.50",
                    "currency": "RUB",
                    "description": "Bridge push",
                    "status": "Подтверждена",
                },
                "visible_row": {
                    "_account_id": account["id"],
                    "_category_id": category["id"],
                },
            }
        ]
    }
    nonce = str(uuid.uuid4())
    pushed = _bridge_post(
        client,
        "push",
        str(binding["id"]),
        str(binding["secret"]),
        payload,
        nonce=nonce,
    )
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["results"][0]["status"] == "applied"
    transaction_id = pushed.json()["results"][0]["result"]["entity_id"]

    replay = _bridge_post(
        client,
        "push",
        str(binding["id"]),
        str(binding["secret"]),
        payload,
        nonce=nonce,
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "APPS_SCRIPT_REPLAY_DETECTED"

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    invalid_headers = _signed_headers(
        str(binding["id"]),
        str(binding["secret"]),
        body,
        nonce=str(uuid.uuid4()),
    )
    invalid_headers["X-Finspace-Body-SHA256"] = "0" * 64
    invalid = client.post(
        "/api/v1/google-sheets/apps-script/push",
        content=body,
        headers=invalid_headers,
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "APPS_SCRIPT_BODY_HASH_INVALID"

    invalid_signature_headers = _signed_headers(
        str(binding["id"]),
        str(binding["secret"]),
        body,
        nonce=str(uuid.uuid4()),
    )
    invalid_signature_headers["X-Finspace-Signature"] = "0" * 64
    invalid_signature = client.post(
        "/api/v1/google-sheets/apps-script/push",
        content=body,
        headers=invalid_signature_headers,
    )
    assert invalid_signature.status_code == 401
    assert invalid_signature.json()["error"]["code"] == "APPS_SCRIPT_SIGNATURE_INVALID"

    changed = json.loads(json.dumps(payload))
    changed["events"][0]["event_id"] = str(uuid.uuid4())
    changed["events"][0]["entity_id"] = transaction_id
    changed["events"][0]["expected_version"] = 1
    changed["events"][0]["changed_fields"] = {"amount": "360.00"}
    pushed_update = _bridge_post(
        client,
        "push",
        str(binding["id"]),
        str(binding["secret"]),
        changed,
    )
    assert pushed_update.status_code == 200
    assert pushed_update.json()["results"][0]["status"] == "applied"
    assert pushed_update.json()["results"][0]["result"]["version"] == 2

    updated = client.patch(
        f"/api/v1/transactions/{transaction_id}",
        headers=headers,
        json={"version": 2, "amount": "375.75"},
    )
    assert updated.status_code == 200, updated.text
    stale = json.loads(json.dumps(payload))
    stale["events"][0]["event_id"] = str(uuid.uuid4())
    stale["events"][0]["entity_id"] = transaction_id
    stale["events"][0]["expected_version"] = 1
    stale["events"][0]["changed_fields"] = {"amount": "400.00"}
    conflict = _bridge_post(
        client,
        "push",
        str(binding["id"]),
        str(binding["secret"]),
        stale,
    )
    assert conflict.status_code == 200
    assert conflict.json()["results"][0]["status"] == "conflict"

    wrong_sheet = json.loads(json.dumps(payload))
    wrong_sheet["events"][0]["event_id"] = str(uuid.uuid4())
    wrong_sheet["events"][0]["spreadsheet_id"] = "other-workspace-sheet"
    isolated = _bridge_post(
        client,
        "push",
        str(binding["id"]),
        str(binding["secret"]),
        wrong_sheet,
    )
    assert isolated.status_code == 200
    assert isolated.json()["results"][0]["error_code"] == "WORKSPACE_ACCESS_DENIED"

    pull = _bridge_post(
        client,
        "pull",
        str(binding["id"]),
        str(binding["secret"]),
        {"spreadsheet_id": spreadsheet_id, "limit": 100},
    )
    assert pull.status_code == 200
    account_event = next(
        item for item in pull.json()["events"] if item["entity_id"] == account["id"]
    )
    reconciled = _bridge_post(
        client,
        "reconcile",
        str(binding["id"]),
        str(binding["secret"]),
        {
            "spreadsheet_id": spreadsheet_id,
            "snapshot_id": str(uuid.uuid4()),
            "items": [
                {
                    "entity_type": "account",
                    "entity_id": account["id"],
                    "version": account_event["version"],
                    "row_hash": account_event["row_hash"],
                    "row_number": 2,
                    "sync_status": "SYNCED",
                }
            ],
            "final": True,
        },
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["status"] == "completed"
    assert reconciled.json()["results"]["matched"] == 1
    assert reconciled.json()["results"]["missing_in_sheet"] >= 1

    dirty_reconciled = _bridge_post(
        client,
        "reconcile",
        str(binding["id"]),
        str(binding["secret"]),
        {
            "spreadsheet_id": spreadsheet_id,
            "snapshot_id": str(uuid.uuid4()),
            "items": [
                {
                    "entity_type": "account",
                    "entity_id": account["id"],
                    "version": account_event["version"],
                    "row_hash": account_event["row_hash"],
                    "row_number": 2,
                    "sync_status": "DIRTY",
                }
            ],
            "final": True,
        },
    )
    assert dirty_reconciled.status_code == 200, dirty_reconciled.text
    assert dirty_reconciled.json()["results"]["conflict"] == 1

    async def inspect_runs() -> tuple[int, int]:
        async with AsyncSessionFactory() as session:
            initial = await session.scalar(
                select(SyncRun).where(
                    SyncRun.binding_id == uuid.UUID(str(binding["id"])),
                    SyncRun.run_type == "initial_export",
                )
            )
            reconciliation = await session.scalar(
                select(SyncRun).where(
                    SyncRun.binding_id == uuid.UUID(str(binding["id"])),
                    SyncRun.run_type == "reconciliation",
                )
            )
            assert initial is not None and reconciliation is not None
            return initial.processed_count, reconciliation.processed_count

    initial_count, reconciliation_count = asyncio.run(inspect_runs())
    assert initial_count == 0
    assert reconciliation_count >= 1


def test_oauth_provider_is_optional_and_disabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_bridge(monkeypatch)
    _, headers = _register_user(client)
    oauth = client.get("/api/v1/integrations/google/status", headers=headers)
    assert oauth.status_code == 200
    assert oauth.json()["configured"] is False
    bridge = client.get("/api/v1/google-sheets/status", headers=headers)
    assert bridge.status_code == 200
    assert bridge.json()["configured"] is True
    assert bridge.json()["provider"] == "apps_script_bridge"
