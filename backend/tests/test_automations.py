import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import recurring_rules as recurring_rules_routes
from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.automations import (
    RecurringRule,
    ServiceApiKey,
    TelegramIntent,
    TelegramLinkCode,
)
from app.db.models.google_sync import GoogleSheetBinding, SyncOutbox
from app.db.models.transactions import FinancialTransaction
from app.db.session import AsyncSessionFactory
from app.services import telegram as telegram_service

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _configure_automation_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)


def _register(client: TestClient, label: str = "Automation") -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"automation-{uuid.uuid4()}@example.com",
            "display_name": f"{label} Owner",
            "password": PASSWORD,
            "workspace_name": f"{label} Workspace",
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


def _service_key(
    client: TestClient,
    headers: dict[str, str],
    permissions: list[str],
) -> tuple[dict, str]:
    account = client.post(
        "/api/v1/settings/service-accounts",
        headers=headers,
        json={"name": "Local n8n", "service_type": "n8n", "permissions": permissions},
    )
    assert account.status_code == 201, account.text
    payload = account.json()["service_account"]
    key = client.post(
        f"/api/v1/settings/service-accounts/{payload['id']}/keys",
        headers=headers,
        json={},
    )
    assert key.status_code == 201, key.text
    return payload, key.json()["key"]


def _references(client: TestClient, headers: dict[str, str]) -> tuple[dict, dict, dict]:
    account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Main card",
            "account_type": "debit_card",
            "currency": "RUB",
            "opening_balance": "100000",
            "opening_balance_at": "2026-07-01T00:00:00Z",
        },
    )
    target = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "Savings",
            "account_type": "savings",
            "currency": "RUB",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-01T00:00:00Z",
        },
    )
    category = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": "Groceries", "category_type": "expense"},
    )
    assert account.status_code == target.status_code == category.status_code == 201
    return account.json(), target.json(), category.json()


def _close_july(client: TestClient, headers: dict[str, str]) -> None:
    prepared = client.post("/api/v1/month-close/2026/7/prepare", headers=headers, json={})
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["status"] == "ready", prepared.text
    confirmed = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": f"automation-close-{uuid.uuid4()}"},
        json={
            "version": prepared.json()["version"],
            "confirm": True,
            "prepare_token": prepared.json()["prepare_token"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text


def _expense_rule(account_id: str, category_id: str, **updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Weekly groceries",
        "rule_type": "expense",
        "schedule_rrule": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0",
        "timezone": "Asia/Yekaterinburg",
        "transaction_type": "expense",
        "amount": "1250.25",
        "currency": "RUB",
        "account_id": account_id,
        "category_id": category_id,
        "creation_mode": "draft",
    }
    payload.update(updates)
    return payload


def test_service_key_hash_permissions_rotation_and_idempotency(client: TestClient) -> None:
    identity, headers = _register(client)
    account, key = _service_key(client, headers, ["automation:execute", "automation:read"])
    assert key.startswith("fsk_")
    prefix = key.split(".", 1)[0]

    async def inspect_key() -> tuple[str, str]:
        async with AsyncSessionFactory() as session:
            stored = await session.scalar(
                select(ServiceApiKey).where(ServiceApiKey.key_prefix == prefix)
            )
            assert stored is not None
            return stored.key_hash, stored.key_prefix

    key_hash, stored_prefix = asyncio.run(inspect_key())
    assert stored_prefix == prefix
    assert key_hash == hashlib.sha256(key.encode()).hexdigest()
    assert key not in client.get("/api/v1/settings/service-accounts", headers=headers).text

    invalid = client.post(
        "/api/v1/automation/heartbeat",
        headers={
            "Authorization": "ServiceKey fsk_000000000000.invalid",
            "X-Idempotency-Key": str(uuid.uuid4()),
        },
        json={},
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "SERVICE_KEY_INVALID"

    idempotency_key = f"heartbeat:{identity['workspace']['id']}:test"
    service_headers = {
        "Authorization": f"ServiceKey {key}",
        "X-Idempotency-Key": idempotency_key,
    }
    first = client.post("/api/v1/automation/heartbeat", headers=service_headers, json={})
    second = client.post("/api/v1/automation/heartbeat", headers=service_headers, json={})
    assert first.status_code == second.status_code == 200
    assert first.json()["run"]["id"] == second.json()["run"]["id"]
    assert second.json()["duplicate"] is True

    bearer = client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {key}"})
    assert bearer.status_code == 401

    rotated = client.post(
        f"/api/v1/settings/service-accounts/{account['id']}/rotate-key",
        headers=headers,
        json={},
    )
    assert rotated.status_code == 200
    new_key = rotated.json()["key"]
    rejected = client.post(
        "/api/v1/automation/heartbeat",
        headers={
            "Authorization": f"ServiceKey {key}",
            "X-Idempotency-Key": str(uuid.uuid4()),
        },
        json={},
    )
    assert rejected.status_code == 401
    accepted = client.post(
        "/api/v1/automation/heartbeat",
        headers={
            "Authorization": f"ServiceKey {new_key}",
            "X-Idempotency-Key": str(uuid.uuid4()),
        },
        json={},
    )
    assert accepted.status_code == 200


def test_service_key_expiration_permission_and_workspace_scope(client: TestClient) -> None:
    _, headers = _register(client, "Scoped")
    account, key = _service_key(client, headers, ["automation:read"])
    denied = client.post(
        "/api/v1/automation/heartbeat",
        headers={
            "Authorization": f"ServiceKey {key}",
            "X-Idempotency-Key": str(uuid.uuid4()),
        },
        json={},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "SERVICE_PERMISSION_DENIED"

    expiring = client.post(
        f"/api/v1/settings/service-accounts/{account['id']}/keys",
        headers=headers,
        json={"expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()},
    )
    assert expiring.status_code == 201
    expiring_key = expiring.json()["key"]
    expiring_prefix = expiring_key.split(".", 1)[0]

    async def expire_key() -> None:
        async with AsyncSessionFactory() as session:
            stored = await session.scalar(
                select(ServiceApiKey).where(ServiceApiKey.key_prefix == expiring_prefix)
            )
            assert stored is not None
            stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire_key())
    expired = client.get(
        "/api/v1/automation/recurring-rules/due",
        headers={"Authorization": f"ServiceKey {expiring_key}"},
    )
    assert expired.status_code == 401


def test_recurring_rule_validation_execution_confirmation_and_outbox(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Recurring")
    account, target, category = _references(client, headers)
    invalid = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json=_expense_rule(account["id"], category["id"], schedule_rrule="EVERY MONDAY"),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "RECURRING_RULE_INVALID"

    created = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json=_expense_rule(account["id"], category["id"]),
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["next_run_at"] is not None
    assert rule["amount"] == "1250.2500"

    class SteppedRouteClock:
        calls = 0

        @classmethod
        def now(cls, _timezone: object | None = None) -> datetime:
            cls.calls += 1
            return datetime(2026, 8, 23, 7, 14, 16 + cls.calls, tzinfo=UTC)

    monkeypatch.setattr(recurring_rules_routes, "datetime", SteppedRouteClock)
    run_key = f"manual-recurring:{rule['id']}:acceptance"
    first = client.post(
        f"/api/v1/recurring-rules/{rule['id']}/run-now",
        headers={**headers, "X-Idempotency-Key": run_key},
    )
    second = client.post(
        f"/api/v1/recurring-rules/{rule['id']}/run-now",
        headers={**headers, "X-Idempotency-Key": run_key},
    )
    assert first.status_code == second.status_code == 200, first.text
    assert first.json()["transaction_id"] == second.json()["transaction_id"]
    assert second.json()["duplicate"] is True
    transaction_id = first.json()["transaction_id"]
    transaction = client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
    assert transaction.json()["status"] == "draft"

    async def add_binding() -> None:
        async with AsyncSessionFactory() as session:
            session.add(
                GoogleSheetBinding(
                    workspace_id=uuid.UUID(identity["workspace"]["id"]),
                    provider="apps_script_bridge",
                    spreadsheet_name="Automation test",
                    template_version=1,
                    status="active",
                    sync_enabled=True,
                    sync_mode="bidirectional",
                    apps_script_enabled=True,
                    binding_secret_hash="0" * 64,
                    binding_secret_created_at=datetime.now(UTC),
                    created_by=uuid.UUID(identity["user"]["id"]),
                )
            )
            await session.commit()

    asyncio.run(add_binding())
    confirmed = client.post(
        f"/api/v1/transactions/{transaction_id}/confirm",
        headers=headers,
        json={"version": transaction.json()["version"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    transfer = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json={
            **_expense_rule(account["id"], category["id"]),
            "name": "Savings transfer",
            "rule_type": "transfer",
            "transaction_type": "transfer",
            "target_account_id": target["id"],
            "category_id": None,
            "creation_mode": "confirmed",
        },
    )
    assert transfer.status_code == 201, transfer.text
    transfer_run = client.post(
        f"/api/v1/recurring-rules/{transfer.json()['id']}/run-now", headers=headers
    )
    assert transfer_run.status_code == 200
    assert transfer_run.json()["status"] == "confirmed_created"

    reminder = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json=_expense_rule(
            account["id"], category["id"], name="Reminder", creation_mode="reminder_only"
        ),
    )
    reminder_run = client.post(
        f"/api/v1/recurring-rules/{reminder.json()['id']}/run-now", headers=headers
    )
    assert reminder_run.status_code == 200
    assert reminder_run.json()["status"] == "reminder_sent"
    assert reminder_run.json()["transaction_id"] is None

    async def outbox_exists() -> bool:
        async with AsyncSessionFactory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(SyncOutbox).where(SyncOutbox.entity_id == uuid.UUID(transaction_id))
                    )
                ).all()
            )
            return bool(rows)

    assert asyncio.run(outbox_exists())


def test_recurring_backdated_execution_is_rejected_by_month_close(
    client: TestClient,
) -> None:
    _, headers = _register(client, "Recurring Closed")
    account, _, category = _references(client, headers)
    created = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json=_expense_rule(
            account["id"],
            category["id"],
            creation_mode="confirmed",
        ),
    )
    assert created.status_code == 201, created.text
    _, service_key = _service_key(client, headers, ["recurring:execute"])
    scheduled_for = datetime(2026, 7, 15, 7, tzinfo=UTC)

    async def backdate_rule() -> None:
        async with AsyncSessionFactory() as session:
            rule = await session.get(
                RecurringRule,
                uuid.UUID(created.json()["id"]),
            )
            assert rule is not None
            rule.next_run_at = scheduled_for
            await session.commit()

    asyncio.run(backdate_rule())
    _close_july(client, headers)
    before = client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]
    response = client.post(
        f"/api/v1/automation/recurring-rules/{created.json()['id']}/execute",
        headers={
            "Authorization": f"ServiceKey {service_key}",
            "X-Idempotency-Key": f"closed-recurring-{uuid.uuid4()}",
        },
        json={"scheduled_for": scheduled_for.isoformat()},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "MONTH_CLOSED"
    assert client.get("/api/v1/transactions", headers=headers).json()["page"]["total"] == before
    history = client.get(f"/api/v1/recurring-rules/{created.json()['id']}/history", headers=headers)
    assert history.status_code == 200
    assert history.json()["page"]["total"] == 1
    assert history.json()["items"][0]["status"] == "failed"
    assert history.json()["items"][0]["transaction_id"] is None


def test_telegram_link_parser_intent_callback_and_isolation(client: TestClient) -> None:
    _, headers = _register(client, "Telegram")
    _references(client, headers)
    _, key = _service_key(client, headers, ["notifications:send"])
    service_headers = {"Authorization": f"ServiceKey {key}"}

    code_response = client.post("/api/v1/settings/telegram/link-code", headers=headers)
    assert code_response.status_code == 201
    code = code_response.json()["code"]

    link_payload = {
        "code": code,
        "telegram_user_id": 100001,
        "telegram_chat_id": 200001,
        "telegram_username": "display_only",
    }
    linked = client.post(
        "/api/v1/integrations/telegram/link",
        headers={**service_headers, "X-Idempotency-Key": "telegram-link:100001"},
        json=link_payload,
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["response_type"] == "linked"
    telegram_status = client.get("/api/v1/settings/telegram", headers=headers).json()
    assert telegram_status["linked"] is True
    assert telegram_status["workspace_id"] == headers["X-Workspace-ID"]

    queued = client.post("/api/v1/settings/notifications/telegram/test", headers=headers)
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued"
    claim_headers = {
        **service_headers,
        "X-Idempotency-Key": "notification-claim:telegram:acceptance",
    }
    claimed = client.post("/api/v1/automation/notifications/pending/claim", headers=claim_headers)
    repeated_claim = client.post(
        "/api/v1/automation/notifications/pending/claim", headers=claim_headers
    )
    assert claimed.status_code == repeated_claim.status_code == 200, claimed.text
    assert len(claimed.json()["items"]) == 1
    assert repeated_claim.json() == {"items": [], "duplicate": True}
    pending_notification = claimed.json()["items"][0]
    assert pending_notification["telegram_user_id"] == 100001
    assert pending_notification["telegram_chat_id"] == 200001
    delivery_headers = {
        **service_headers,
        "X-Idempotency-Key": "telegram-delivery:test:acceptance",
    }
    delivery_payload = {
        "telegram_user_id": 100001,
        "telegram_chat_id": 200001,
        "delivery_id": pending_notification["opaque_id"],
        "status": "sent",
    }
    delivery = client.post(
        "/api/v1/integrations/telegram/delivery-status",
        headers=delivery_headers,
        json=delivery_payload,
    )
    repeated_delivery = client.post(
        "/api/v1/integrations/telegram/delivery-status",
        headers=delivery_headers,
        json=delivery_payload,
    )
    assert delivery.status_code == repeated_delivery.status_code == 200, delivery.text
    assert delivery.json()["status"] == "sent"
    assert repeated_delivery.json()["duplicate"] is True

    reused = client.post(
        "/api/v1/integrations/telegram/link",
        headers={**service_headers, "X-Idempotency-Key": "telegram-link:reuse"},
        json=link_payload,
    )
    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "TELEGRAM_LINK_CODE_INVALID"

    unknown = client.post(
        "/api/v1/integrations/telegram/message",
        headers={**service_headers, "X-Idempotency-Key": "telegram-message:unknown"},
        json={
            "telegram_user_id": 999999,
            "telegram_chat_id": 999999,
            "text": "/accounts",
        },
    )
    assert unknown.status_code == 403
    assert unknown.json()["error"]["code"] == "TELEGRAM_NOT_LINKED"

    message_headers = {
        **service_headers,
        "X-Idempotency-Key": "telegram-message:100001:1",
    }
    preview = client.post(
        "/api/v1/integrations/telegram/message",
        headers=message_headers,
        json={
            "telegram_user_id": 100001,
            "telegram_chat_id": 200001,
            "text": "расход 1250,25 groceries main card",
            "update_id": 1,
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["response_type"] == "preview"
    callback_data = preview.json()["buttons"][0]["callback_data"]
    assert len(callback_data) <= 32
    assert "1250" not in callback_data

    wrong_callback = client.post(
        "/api/v1/integrations/telegram/callback",
        headers={**service_headers, "X-Idempotency-Key": "telegram-callback:wrong"},
        json={
            "telegram_user_id": 100001,
            "telegram_chat_id": 999999,
            "opaque_id": callback_data,
        },
    )
    assert wrong_callback.status_code == 403

    callback_headers = {
        **service_headers,
        "X-Idempotency-Key": "telegram-callback:100001:1",
    }
    confirmed = client.post(
        "/api/v1/integrations/telegram/callback",
        headers=callback_headers,
        json={
            "telegram_user_id": 100001,
            "telegram_chat_id": 200001,
            "opaque_id": callback_data,
            "update_id": 2,
        },
    )
    repeated = client.post(
        "/api/v1/integrations/telegram/callback",
        headers=callback_headers,
        json={
            "telegram_user_id": 100001,
            "telegram_chat_id": 200001,
            "opaque_id": callback_data,
            "update_id": 2,
        },
    )
    assert confirmed.status_code == repeated.status_code == 200, confirmed.text
    assert confirmed.json()["transaction_id"] == repeated.json()["transaction_id"]
    assert repeated.json()["duplicate"] is True

    async def inspect_telegram() -> tuple[str, int, int, bool]:
        async with AsyncSessionFactory() as session:
            transaction = await session.get(
                FinancialTransaction, uuid.UUID(confirmed.json()["transaction_id"])
            )
            assert transaction is not None
            intents = list((await session.scalars(select(TelegramIntent))).all())
            codes = list((await session.scalars(select(TelegramLinkCode))).all())
            serialized = repr([(item.payload, item.opaque_id) for item in intents])
            assert "bot_token" not in serialized.casefold()
            return (
                str(transaction.amount),
                len(intents),
                sum(item.attempts for item in codes),
                transaction.payee_id is None,
            )

    amount, intent_count, attempts, payee_is_unset = asyncio.run(inspect_telegram())
    assert amount == "1250.2500"
    assert intent_count >= 2
    assert attempts == 0
    assert payee_is_unset is True


def test_telegram_backdated_confirmation_is_terminal_month_closed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, headers = _register(client, "Telegram Closed")
    _references(client, headers)
    _, key = _service_key(client, headers, ["notifications:send"])
    service_headers = {"Authorization": f"ServiceKey {key}"}
    link_code = client.post("/api/v1/settings/telegram/link-code", headers=headers).json()["code"]
    linked = client.post(
        "/api/v1/integrations/telegram/link",
        headers={**service_headers, "X-Idempotency-Key": f"closed-link-{uuid.uuid4()}"},
        json={
            "code": link_code,
            "telegram_user_id": 300001,
            "telegram_chat_id": 400001,
            "telegram_username": "closed_test",
        },
    )
    assert linked.status_code == 200, linked.text
    _close_july(client, headers)

    class ClosedPeriodClock:
        @classmethod
        def now(cls, _timezone: object | None = None) -> datetime:
            return datetime(2026, 7, 20, 10, tzinfo=UTC)

    monkeypatch.setattr(telegram_service, "datetime", ClosedPeriodClock)
    preview = client.post(
        "/api/v1/integrations/telegram/message",
        headers={**service_headers, "X-Idempotency-Key": f"closed-message-{uuid.uuid4()}"},
        json={
            "telegram_user_id": 300001,
            "telegram_chat_id": 400001,
            "text": "расход 1250,25 groceries main card",
            "update_id": 10,
        },
    )
    assert preview.status_code == 200, preview.text
    callback_data = preview.json()["buttons"][0]["callback_data"]
    before = client.get("/api/v1/transactions", headers=headers).json()["page"]["total"]
    rejected = client.post(
        "/api/v1/integrations/telegram/callback",
        headers={**service_headers, "X-Idempotency-Key": f"closed-callback-{uuid.uuid4()}"},
        json={
            "telegram_user_id": 300001,
            "telegram_chat_id": 400001,
            "opaque_id": callback_data,
            "update_id": 11,
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "MONTH_CLOSED"
    assert client.get("/api/v1/transactions", headers=headers).json()["page"]["total"] == before


def _verified_backup() -> None:
    async def create_audit() -> None:
        sha = "a" * 64
        async with AsyncSessionFactory() as session:
            session.add_all(
                [
                    AuditLog(
                        workspace_id=None,
                        actor_user_id=None,
                        entity_type="backup",
                        entity_id=uuid.uuid4(),
                        action="backup.created",
                        before_data=None,
                        after_data={"filename": "finspace_test.dump", "sha256": sha},
                        request_id=None,
                        source="system",
                    ),
                    AuditLog(
                        workspace_id=None,
                        actor_user_id=None,
                        entity_type="backup",
                        entity_id=uuid.uuid4(),
                        action="backup.verified",
                        before_data=None,
                        after_data={"filename": "finspace_test.dump", "sha256": sha},
                        request_id=None,
                        source="system",
                    ),
                ]
            )
            await session.commit()

    asyncio.run(create_audit())


def test_weekly_and_uncategorized_reports_keep_currencies_separate(client: TestClient) -> None:
    identity, headers = _register(client, "Reports")
    account, _, category = _references(client, headers)
    income_category = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": "Salary", "category_type": "income"},
    ).json()
    usd_account = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": "USD cash",
            "account_type": "cash",
            "currency": "USD",
            "opening_balance": "0",
            "opening_balance_at": "2026-07-01T00:00:00Z",
        },
    ).json()
    for payload in (
        {
            "occurred_at": "2026-07-20T10:00:00Z",
            "transaction_type": "expense",
            "amount": "250.50",
            "currency": "RUB",
            "account_id": account["id"],
            "category_id": category["id"],
        },
        {
            "occurred_at": "2026-07-21T10:00:00Z",
            "transaction_type": "income",
            "amount": "1000.00",
            "currency": "RUB",
            "account_id": account["id"],
            "category_id": income_category["id"],
        },
        {
            "occurred_at": "2026-07-22T10:00:00Z",
            "transaction_type": "expense",
            "amount": "10.25",
            "currency": "USD",
            "account_id": usd_account["id"],
            "category_id": category["id"],
        },
    ):
        response = client.post("/api/v1/transactions", headers=headers, json=payload)
        assert response.status_code == 201, response.text

    async def add_uncategorized() -> None:
        async with AsyncSessionFactory() as session:
            session.add(
                FinancialTransaction(
                    workspace_id=uuid.UUID(identity["workspace"]["id"]),
                    occurred_at=datetime(2026, 7, 23, 10, tzinfo=UTC),
                    transaction_type="expense",
                    amount=Decimal("5.50"),
                    currency="USD",
                    account_id=uuid.UUID(usd_account["id"]),
                    category_id=None,
                    status="confirmed",
                    source="manual",
                    created_by=uuid.UUID(identity["user"]["id"]),
                    updated_by=uuid.UUID(identity["user"]["id"]),
                )
            )
            await session.commit()

    asyncio.run(add_uncategorized())
    _verified_backup()
    _, key = _service_key(client, headers, ["reports:generate"])
    service_headers = {
        "Authorization": f"ServiceKey {key}",
        "X-Idempotency-Key": "weekly-report:reports:2026-W30",
    }
    report = client.post(
        "/api/v1/automation/reports/weekly",
        headers=service_headers,
        json={"workspace_id": identity["workspace"]["id"], "week_start": "2026-07-20"},
    )
    assert report.status_code == 200, report.text
    groups = {item["currency"]: item for item in report.json()["groups"]}
    assert groups["RUB"]["income"] == "1000.0000"
    assert groups["RUB"]["expense"] == "250.5000"
    assert groups["USD"]["expense"] == "15.7500"
    assert all(len(message) <= 4000 for message in report.json()["messages"])

    uncategorized = client.post(
        "/api/v1/automation/reports/uncategorized",
        headers={
            "Authorization": f"ServiceKey {key}",
            "X-Idempotency-Key": "uncategorized:reports:2026-07-23",
        },
        json={"workspace_id": identity["workspace"]["id"]},
    )
    assert uncategorized.status_code == 200, uncategorized.text
    assert uncategorized.json()["count"] == 1
    assert uncategorized.json()["totals"] == [{"currency": "USD", "amount": "5.5000"}]


def test_month_close_blocks_confirms_and_requires_explicit_reopen(client: TestClient) -> None:
    _, headers = _register(client, "Month Close")
    account, _, category = _references(client, headers)
    _verified_backup()
    draft = client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": "2026-07-10T10:00:00Z",
            "transaction_type": "expense",
            "amount": "100.00",
            "currency": "RUB",
            "account_id": account["id"],
            "category_id": category["id"],
            "status": "draft",
        },
    )
    assert draft.status_code == 201
    blocked = client.post("/api/v1/month-close/2026/7/prepare", headers=headers, json={})
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"
    assert any(item["code"] == "DRAFT_TRANSACTIONS" for item in blocked.json()["blocking_issues"])

    confirmed_draft = client.post(
        f"/api/v1/transactions/{draft.json()['id']}/confirm",
        headers=headers,
        json={"version": draft.json()["version"]},
    )
    assert confirmed_draft.status_code == 200
    ready = client.post("/api/v1/month-close/2026/7/prepare", headers=headers, json={})
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready"

    stale = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": "month-close:stale"},
        json={
            "version": ready.json()["version"] - 1,
            "confirm": True,
            "prepare_token": ready.json()["prepare_token"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "MONTH_CLOSE_VERSION_CONFLICT"
    closed = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": "month-close:confirm"},
        json={
            "version": ready.json()["version"],
            "confirm": True,
            "prepare_token": ready.json()["prepare_token"],
        },
    )
    repeated = client.post(
        "/api/v1/month-close/2026/7/confirm",
        headers={**headers, "X-Idempotency-Key": "month-close:confirm"},
        json={
            "version": ready.json()["version"],
            "confirm": True,
            "prepare_token": ready.json()["prepare_token"],
        },
    )
    assert closed.status_code == repeated.status_code == 200, closed.text
    assert closed.json()["status"] == "confirmed"

    late = client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "occurred_at": "2026-07-28T10:00:00Z",
            "transaction_type": "expense",
            "amount": "75.00",
            "currency": "RUB",
            "account_id": account["id"],
            "category_id": category["id"],
        },
    )
    assert late.status_code == 409, late.text
    assert late.json()["error"]["code"] == "MONTH_CLOSED"

    still_closed = client.get("/api/v1/month-close/2026/7", headers=headers)
    assert still_closed.status_code == 200
    assert still_closed.json()["status"] == "confirmed"

    reopened = client.post(
        "/api/v1/month-close/2026/7/reopen",
        headers={**headers, "X-Idempotency-Key": "month-close:reopen"},
        json={"version": still_closed.json()["version"], "reason": "Late correction"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "reopened"
