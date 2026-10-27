import asyncio
import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.account_reconciliation import AccountReconciliation
from app.db.models.accounts import Account
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory
from app.dependencies.context import RequestContext
from app.schemas.account_reconciliation import AccountReconciliationConfirmRequest
from app.schemas.transactions import TransactionCreate
from app.services import account_reconciliation as reconciliation_service
from app.services import transactions as transaction_service


def _bootstrap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)

    async def create_identity() -> dict[str, str]:
        async with AsyncSessionFactory() as session:
            unique = uuid.uuid4().hex
            user = User(
                email=f"reconciliation-{unique}@test.local",
                normalized_email=f"reconciliation-{unique}@test.local",
                display_name="Reconciliation owner",
                timezone="UTC",
            )
            session.add(user)
            await session.flush()
            workspace = Workspace(
                name=f"Reconciliation {unique[:8]}",
                base_currency="RUB",
                timezone="UTC",
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


def _set_workspace_timezone(workspace_id: str, timezone: str) -> None:
    async def update() -> None:
        async with AsyncSessionFactory() as session:
            workspace = await session.get(Workspace, uuid.UUID(workspace_id))
            assert workspace is not None
            workspace.timezone = timezone
            await session.commit()

    asyncio.run(update())


def _account(
    client: TestClient,
    headers: dict[str, str],
    *,
    opening_balance: str = "100.0000",
    opening_balance_at: str = "2026-01-01T00:00:00Z",
    currency: str = "RUB",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": f"Reconciliation {uuid.uuid4().hex[:8]}",
            "account_type": "debit_card",
            "currency": currency,
            "opening_balance": opening_balance,
            "opening_balance_at": opening_balance_at,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _category(client: TestClient, headers: dict[str, str], category_type: str) -> str:
    response = client.post(
        "/api/v1/categories",
        headers=headers,
        json={
            "name": f"Reconciliation {category_type} {uuid.uuid4().hex[:8]}",
            "category_type": category_type,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _transaction(
    client: TestClient,
    headers: dict[str, str],
    *,
    account_id: str,
    amount: str,
    transaction_type: str = "income",
    occurred_at: str = "2026-02-10T12:00:00Z",
    status: str = "confirmed",
    target_account_id: str | None = None,
    currency: str = "RUB",
    related_transaction_id: str | None = None,
    comment: str | None = None,
) -> dict[str, object]:
    category_id = None
    if transaction_type in {"income", "expense"}:
        category_id = _category(client, headers, transaction_type)
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
            "comment": comment,
            "status": status,
            "source": "manual",
            "splits": [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _preview(
    client: TestClient,
    headers: dict[str, str],
    account: dict[str, object],
    *,
    statement_balance: str,
    statement_date: str = "2026-02-28",
    currency: str = "RUB",
) -> object:
    return client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/preview",
        headers=headers,
        json={
            "statement_date": statement_date,
            "statement_balance": statement_balance,
            "currency": currency,
            "account_version": account["version"],
        },
    )


def _confirm_payload(
    account: dict[str, object], preview: dict[str, object], *, key: str | None = None
) -> dict[str, object]:
    return {
        "statement_date": preview["statement_date"],
        "statement_balance": preview["statement_balance"],
        "currency": preview["currency"],
        "account_version": account["version"],
        "preview_token": preview["preview_token"],
        "idempotency_key": key or f"reconciliation-{uuid.uuid4()}",
    }


def test_exact_match_confirmation_history_and_idempotency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers)
    income = _transaction(client, headers, account_id=str(account["id"]), amount="25")
    expense = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        amount="10",
        transaction_type="expense",
    )

    preview_response = _preview(client, headers, account, statement_balance="115")
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["calculated_balance"] == "115.0000"
    assert preview["difference"] == "0.0000"
    assert {item["id"] for item in preview["transactions"]} == {income["id"], expense["id"]}

    payload = _confirm_payload(account, preview, key=f"exact-{uuid.uuid4()}")
    confirmed = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json=payload,
    )
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["status"] == "confirmed"
    assert set(result["transaction_ids"]) == {income["id"], expense["id"]}

    retry = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json=payload,
    )
    assert retry.status_code == 200
    assert retry.json()["id"] == result["id"]

    conflict_payload = {**payload, "statement_balance": "116.0000"}
    conflict = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json=conflict_payload,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    history = client.get(f"/api/v1/accounts/{account['id']}/reconciliations", headers=headers)
    detail = client.get(
        f"/api/v1/accounts/{account['id']}/reconciliations/{result['id']}",
        headers=headers,
    )
    assert history.status_code == 200
    assert history.json()["page"]["total"] == 1
    assert detail.status_code == 200
    assert detail.json()["id"] == result["id"]

    transaction_update = client.patch(
        f"/api/v1/transactions/{income['id']}",
        headers=headers,
        json={"version": income["version"] + 1, "description": "Must not change"},
    )
    assert transaction_update.status_code == 409
    assert transaction_update.json()["error"]["code"] == "RECONCILED_TRANSACTION_IMMUTABLE"


def test_positive_negative_difference_currency_and_stale_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers)

    positive = _preview(client, headers, account, statement_balance="105")
    negative = _preview(client, headers, account, statement_balance="95")
    wrong_currency = _preview(client, headers, account, statement_balance="100", currency="USD")
    assert positive.status_code == 200
    assert positive.json()["difference"] == "5.0000"
    assert negative.status_code == 200
    assert negative.json()["difference"] == "-5.0000"
    assert wrong_currency.status_code == 422
    assert wrong_currency.json()["error"]["code"] == "CURRENCY_MISMATCH"

    cannot_confirm = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(account, positive.json()),
    )
    assert cannot_confirm.status_code == 409
    assert cannot_confirm.json()["error"]["code"] == "RECONCILIATION_DIFFERENCE"

    exact = _preview(client, headers, account, statement_balance="100").json()
    update = client.patch(
        f"/api/v1/accounts/{account['id']}",
        headers=headers,
        json={"version": account["version"], "description": "Concurrent edit"},
    )
    assert update.status_code == 200
    stale = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(account, exact),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_draft_cancelled_already_reconciled_and_transfer_per_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    source = _account(client, headers, opening_balance="100")
    target = _account(client, headers, opening_balance="50")
    draft = _transaction(client, headers, account_id=str(source["id"]), amount="20", status="draft")
    cancelled = _transaction(
        client, headers, account_id=str(source["id"]), amount="30", status="confirmed"
    )
    cancelled_response = client.post(
        f"/api/v1/transactions/{cancelled['id']}/cancel",
        headers=headers,
        json={"version": cancelled["version"]},
    )
    assert cancelled_response.status_code == 200
    transfer = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        target_account_id=str(target["id"]),
        amount="10",
        transaction_type="transfer",
    )

    legacy_category_id = uuid.UUID(_category(client, headers, "income"))

    async def create_legacy_reconciled() -> uuid.UUID:
        async with AsyncSessionFactory() as session:
            transaction = FinancialTransaction(
                workspace_id=uuid.UUID(headers["X-Workspace-ID"]),
                occurred_at=datetime.fromisoformat("2026-02-11T12:00:00+00:00"),
                transaction_type="income",
                amount=Decimal("5"),
                currency="RUB",
                account_id=uuid.UUID(str(source["id"])),
                category_id=legacy_category_id,
                status="reconciled",
                source="manual",
                created_by=uuid.UUID(headers["X-User-ID"]),
                updated_by=uuid.UUID(headers["X-User-ID"]),
            )
            session.add(transaction)
            await session.commit()
            return transaction.id

    legacy_id = asyncio.run(create_legacy_reconciled())
    source_preview_response = _preview(client, headers, source, statement_balance="95")
    assert source_preview_response.status_code == 200, source_preview_response.text
    source_preview = source_preview_response.json()
    assert source_preview["calculated_balance"] == "95.0000"
    assert [item["id"] for item in source_preview["transactions"]] == [transfer["id"]]
    assert str(legacy_id) not in {item["id"] for item in source_preview["transactions"]}
    assert draft["id"] not in {item["id"] for item in source_preview["transactions"]}
    assert cancelled["id"] not in {item["id"] for item in source_preview["transactions"]}

    source_confirm = client.post(
        f"/api/v1/accounts/{source['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(source, source_preview),
    )
    assert source_confirm.status_code == 200, source_confirm.text

    target_preview_response = _preview(client, headers, target, statement_balance="60")
    assert target_preview_response.status_code == 200, target_preview_response.text
    target_preview = target_preview_response.json()
    assert [item["id"] for item in target_preview["transactions"]] == [transfer["id"]]
    target_confirm = client.post(
        f"/api/v1/accounts/{target['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(target, target_preview),
    )
    assert target_confirm.status_code == 200, target_confirm.text


def test_transfer_global_lock_and_independent_account_sides(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    source = _account(client, headers, opening_balance="100")
    target = _account(client, headers, opening_balance="50")
    replacement_source = _account(client, headers, opening_balance="0")
    replacement_target = _account(client, headers, opening_balance="0")
    transfer = _transaction(
        client,
        headers,
        account_id=str(source["id"]),
        target_account_id=str(target["id"]),
        amount="10",
        transaction_type="transfer",
    )

    source_preview = _preview(client, headers, source, statement_balance="90").json()
    assert [item["id"] for item in source_preview["transactions"]] == [transfer["id"]]
    source_confirm = client.post(
        f"/api/v1/accounts/{source['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(source, source_preview),
    )
    assert source_confirm.status_code == 200, source_confirm.text

    protected_transfer_response = client.get(
        f"/api/v1/transactions/{transfer['id']}", headers=headers
    )
    assert protected_transfer_response.status_code == 200
    protected_transfer = protected_transfer_response.json()
    assert protected_transfer["status"] == "reconciled"

    stale_edit = client.patch(
        f"/api/v1/transactions/{transfer['id']}",
        headers=headers,
        json={"version": transfer["version"], "amount": "11.0000"},
    )
    assert stale_edit.status_code == 409
    assert stale_edit.json()["error"]["code"] == "VERSION_CONFLICT"

    protected_mutations = [
        {"version": protected_transfer["version"], "amount": "11.0000"},
        {
            "version": protected_transfer["version"],
            "account_id": replacement_source["id"],
        },
        {
            "version": protected_transfer["version"],
            "target_account_id": replacement_target["id"],
        },
    ]
    for mutation in protected_mutations:
        response = client.patch(
            f"/api/v1/transactions/{transfer['id']}",
            headers=headers,
            json=mutation,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RECONCILED_TRANSACTION_IMMUTABLE"

    cancel = client.post(
        f"/api/v1/transactions/{transfer['id']}/cancel",
        headers=headers,
        json={"version": protected_transfer["version"]},
    )
    delete = client.delete(
        f"/api/v1/transactions/{transfer['id']}",
        headers=headers,
        params={"version": protected_transfer["version"]},
    )
    for response in (cancel, delete):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RECONCILED_TRANSACTION_IMMUTABLE"

    current_source = client.get(f"/api/v1/accounts/{source['id']}", headers=headers).json()
    repeated_source = _preview(client, headers, current_source, statement_balance="90")
    assert repeated_source.status_code == 200, repeated_source.text
    assert repeated_source.json()["calculated_balance"] == "90.0000"
    assert repeated_source.json()["transactions"] == []

    target_preview = _preview(client, headers, target, statement_balance="60")
    assert target_preview.status_code == 200, target_preview.text
    assert target_preview.json()["calculated_balance"] == "60.0000"
    assert [item["id"] for item in target_preview.json()["transactions"]] == [transfer["id"]]
    target_confirm = client.post(
        f"/api/v1/accounts/{target['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(target, target_preview.json()),
    )
    assert target_confirm.status_code == 200, target_confirm.text

    current_source = client.get(f"/api/v1/accounts/{source['id']}", headers=headers).json()
    current_target = client.get(f"/api/v1/accounts/{target['id']}", headers=headers).json()
    assert (
        _preview(client, headers, current_source, statement_balance="90").json()["transactions"]
        == []
    )
    assert (
        _preview(client, headers, current_target, statement_balance="60").json()["transactions"]
        == []
    )


def test_workspace_timezone_boundary(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _bootstrap(client, monkeypatch)
    _set_workspace_timezone(headers["X-Workspace-ID"], "Asia/Yekaterinburg")
    try:
        account = _account(client, headers, opening_balance="100")
        included = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="10",
            occurred_at="2026-02-28T18:59:59Z",
        )
        excluded = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="20",
            occurred_at="2026-02-28T19:00:00Z",
        )
        preview_response = _preview(
            client,
            headers,
            account,
            statement_balance="110",
            statement_date="2026-02-28",
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["calculated_balance"] == "110.0000"
        assert [item["id"] for item in preview["transactions"]] == [included["id"]]
        assert excluded["id"] not in {item["id"] for item in preview["transactions"]}
        assert preview["cutoff_at"] == "2026-02-28T19:00:00Z"
    finally:
        _set_workspace_timezone(headers["X-Workspace-ID"], "UTC")


def test_money_type_status_cutoff_dst_and_non_rub_regression(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    _set_workspace_timezone(headers["X-Workspace-ID"], "America/New_York")
    try:
        account = _account(client, headers, currency="USD", opening_balance="100")
        income = _transaction(
            client, headers, account_id=str(account["id"]), amount="40", currency="USD"
        )
        expense = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="10",
            currency="USD",
            transaction_type="expense",
        )
        refund = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="3",
            currency="USD",
            transaction_type="refund",
            related_transaction_id=str(expense["id"]),
        )
        adjustment = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="2",
            currency="USD",
            transaction_type="adjustment",
            comment="Explicit correction",
        )
        draft = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="30",
            currency="USD",
            status="draft",
        )
        cancelled = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="40",
            currency="USD",
        )
        cancelled_response = client.post(
            f"/api/v1/transactions/{cancelled['id']}/cancel",
            headers=headers,
            json={"version": cancelled["version"]},
        )
        assert cancelled_response.status_code == 200
        deleted = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="20",
            currency="USD",
        )
        deleted_response = client.delete(
            f"/api/v1/transactions/{deleted['id']}",
            headers=headers,
            params={"version": deleted["version"]},
        )
        assert deleted_response.status_code == 200
        before_cutoff = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="1",
            currency="USD",
            occurred_at="2026-03-09T03:59:59Z",
        )
        at_cutoff = _transaction(
            client,
            headers,
            account_id=str(account["id"]),
            amount="99",
            currency="USD",
            occurred_at="2026-03-09T04:00:00Z",
        )

        legacy_category_id = uuid.UUID(_category(client, headers, "income"))

        async def create_legacy_reconciled() -> uuid.UUID:
            async with AsyncSessionFactory() as session:
                transaction = FinancialTransaction(
                    workspace_id=uuid.UUID(headers["X-Workspace-ID"]),
                    occurred_at=datetime.fromisoformat("2026-02-15T12:00:00+00:00"),
                    transaction_type="income",
                    amount=Decimal("5"),
                    currency="USD",
                    account_id=uuid.UUID(str(account["id"])),
                    category_id=legacy_category_id,
                    status="reconciled",
                    source="manual",
                    created_by=uuid.UUID(headers["X-User-ID"]),
                    updated_by=uuid.UUID(headers["X-User-ID"]),
                )
                session.add(transaction)
                await session.commit()
                return transaction.id

        legacy_id = asyncio.run(create_legacy_reconciled())
        preview_response = _preview(
            client,
            headers,
            account,
            statement_balance="141",
            statement_date="2026-03-08",
            currency="USD",
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["cutoff_at"] == "2026-03-09T04:00:00Z"
        assert preview["currency"] == "USD"
        assert preview["calculated_balance"] == "141.0000"
        assert preview["difference"] == "0.0000"
        candidate_ids = {item["id"] for item in preview["transactions"]}
        assert candidate_ids == {
            income["id"],
            expense["id"],
            refund["id"],
            adjustment["id"],
            before_cutoff["id"],
        }
        assert {
            draft["id"],
            cancelled["id"],
            deleted["id"],
            at_cutoff["id"],
            str(legacy_id),
        }.isdisjoint(candidate_ids)
    finally:
        _set_workspace_timezone(headers["X-Workspace-ID"], "UTC")


def test_reconciliation_does_not_follow_refund_reference_across_workspaces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers)
    expense = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        amount="10",
        transaction_type="expense",
    )
    refund = _transaction(
        client,
        headers,
        account_id=str(account["id"]),
        amount="5",
        transaction_type="refund",
        related_transaction_id=str(expense["id"]),
    )

    other_headers = _bootstrap(client, monkeypatch)
    other_account = _account(client, other_headers)
    foreign_original = _transaction(
        client,
        other_headers,
        account_id=str(other_account["id"]),
        amount="1",
        transaction_type="expense",
    )

    async def corrupt_reference() -> None:
        async with AsyncSessionFactory() as session:
            current_refund = await session.get(FinancialTransaction, uuid.UUID(str(refund["id"])))
            assert current_refund is not None
            current_refund.related_transaction_id = uuid.UUID(str(foreign_original["id"]))
            await session.commit()

    asyncio.run(corrupt_reference())

    preview = _preview(client, headers, account, statement_balance="90")
    assert preview.status_code == 200, preview.text
    assert preview.json()["calculated_balance"] == "90.0000"
    assert preview.json()["difference"] == "0.0000"


def test_transaction_change_invalidates_preview_and_wrong_account_hides_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers)
    other_account = _account(client, headers)
    transaction = _transaction(client, headers, account_id=str(account["id"]), amount="10")
    preview = _preview(client, headers, account, statement_balance="110").json()
    updated = client.patch(
        f"/api/v1/transactions/{transaction['id']}",
        headers=headers,
        json={"version": transaction["version"], "description": "Changed after preview"},
    )
    assert updated.status_code == 200, updated.text
    stale = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(account, preview),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RECONCILIATION_PREVIEW_STALE"

    fresh_preview = _preview(client, headers, account, statement_balance="110").json()
    confirmed = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(account, fresh_preview),
    )
    assert confirmed.status_code == 200, confirmed.text
    wrong_account = client.get(
        f"/api/v1/accounts/{other_account['id']}/reconciliations/{confirmed.json()['id']}",
        headers=headers,
    )
    assert wrong_account.status_code == 404
    assert wrong_account.json()["error"]["code"] == "ACCOUNT_RECONCILIATION_NOT_FOUND"


def test_tenant_isolation_and_permissions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers)

    async def identities() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        async with AsyncSessionFactory() as session:
            owner = User(
                email=f"owner-{uuid.uuid4()}@test.local",
                normalized_email=f"owner-{uuid.uuid4()}@test.local",
                display_name="Other owner",
            )
            viewer = User(
                email=f"viewer-{uuid.uuid4()}@test.local",
                normalized_email=f"viewer-{uuid.uuid4()}@test.local",
                display_name="Viewer",
            )
            editor = User(
                email=f"editor-{uuid.uuid4()}@test.local",
                normalized_email=f"editor-{uuid.uuid4()}@test.local",
                display_name="Editor",
            )
            session.add_all([owner, viewer, editor])
            await session.flush()
            other_workspace = Workspace(
                name="Other workspace",
                base_currency="RUB",
                timezone="UTC",
                owner_user_id=owner.id,
            )
            session.add(other_workspace)
            await session.flush()
            session.add_all(
                [
                    WorkspaceMember(
                        workspace_id=other_workspace.id, user_id=owner.id, role="owner"
                    ),
                    WorkspaceMember(
                        workspace_id=uuid.UUID(headers["X-Workspace-ID"]),
                        user_id=viewer.id,
                        role="viewer",
                    ),
                    WorkspaceMember(
                        workspace_id=uuid.UUID(headers["X-Workspace-ID"]),
                        user_id=editor.id,
                        role="editor",
                    ),
                ]
            )
            await session.commit()
            return (
                {
                    "X-User-ID": str(owner.id),
                    "X-Workspace-ID": str(other_workspace.id),
                },
                {
                    "X-User-ID": str(viewer.id),
                    "X-Workspace-ID": headers["X-Workspace-ID"],
                },
                {
                    "X-User-ID": str(editor.id),
                    "X-Workspace-ID": headers["X-Workspace-ID"],
                },
            )

    other_headers, viewer_headers, editor_headers = asyncio.run(identities())
    isolated = _preview(client, other_headers, account, statement_balance="100")
    assert isolated.status_code == 404

    preview = _preview(client, viewer_headers, account, statement_balance="100")
    assert preview.status_code == 200
    forbidden = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=viewer_headers,
        json=_confirm_payload(account, preview.json()),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    editor_preview = _preview(client, editor_headers, account, statement_balance="100")
    assert editor_preview.status_code == 200
    editor_confirm = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=editor_headers,
        json=_confirm_payload(account, editor_preview.json()),
    )
    assert editor_confirm.status_code == 200, editor_confirm.text


def test_concurrent_confirmation_allows_only_one_commit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers)
    _transaction(client, headers, account_id=str(account["id"]), amount="10")
    preview = _preview(client, headers, account, statement_balance="110").json()

    async def race() -> list[object]:
        start = asyncio.Event()

        async def confirm(index: int) -> object:
            async with AsyncSessionFactory() as session:
                user = await session.get(User, uuid.UUID(headers["X-User-ID"]))
                workspace = await session.get(Workspace, uuid.UUID(headers["X-Workspace-ID"]))
                assert user is not None and workspace is not None
                context = RequestContext(
                    user=user,
                    workspace=workspace,
                    role="owner",
                    request_id=str(uuid.uuid4()),
                )
                data = AccountReconciliationConfirmRequest.model_validate(
                    _confirm_payload(account, preview, key=f"race-{index}-{uuid.uuid4()}")
                )
                await start.wait()
                try:
                    return await reconciliation_service.confirm_reconciliation(
                        session, context, uuid.UUID(str(account["id"])), data
                    )
                except ApiError as exc:
                    return exc

        tasks = [asyncio.create_task(confirm(index)) for index in range(2)]
        start.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(race())
    assert sum(not isinstance(item, ApiError) for item in results) == 1
    conflicts = [item for item in results if isinstance(item, ApiError)]
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert conflicts[0].code == "VERSION_CONFLICT"


def test_reconciliation_serializes_concurrent_insert_before_cutoff(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers)
    category_id = _category(client, headers, "income")
    _transaction(client, headers, account_id=str(account["id"]), amount="10")
    preview = _preview(client, headers, account, statement_balance="110").json()
    original_effective_transactions = reconciliation_service._effective_transactions

    async def race() -> tuple[bool, object, FinancialTransaction]:
        snapshot_read = asyncio.Event()
        release_confirmation = asyncio.Event()
        insert_finished = asyncio.Event()

        async def paused_effective_transactions(*args: object, **kwargs: object):
            result = await original_effective_transactions(*args, **kwargs)  # type: ignore[arg-type]
            if kwargs.get("for_update"):
                snapshot_read.set()
                await release_confirmation.wait()
            return result

        monkeypatch.setattr(
            reconciliation_service,
            "_effective_transactions",
            paused_effective_transactions,
        )

        async def context_for(session: object) -> RequestContext:
            user = await session.get(User, uuid.UUID(headers["X-User-ID"]))  # type: ignore[attr-defined]
            workspace = await session.get(  # type: ignore[attr-defined]
                Workspace, uuid.UUID(headers["X-Workspace-ID"])
            )
            assert user is not None and workspace is not None
            return RequestContext(
                user=user,
                workspace=workspace,
                role="owner",
                request_id=str(uuid.uuid4()),
            )

        async def confirm() -> object:
            async with AsyncSessionFactory() as session:
                context = await context_for(session)
                data = AccountReconciliationConfirmRequest.model_validate(
                    _confirm_payload(account, preview, key=f"insert-race-{uuid.uuid4()}")
                )
                return await reconciliation_service.confirm_reconciliation(
                    session, context, uuid.UUID(str(account["id"])), data
                )

        async def insert() -> FinancialTransaction:
            async with AsyncSessionFactory() as session:
                context = await context_for(session)
                data = TransactionCreate.model_validate(
                    {
                        "occurred_at": "2026-02-20T12:00:00Z",
                        "transaction_type": "income",
                        "amount": "5.0000",
                        "currency": "RUB",
                        "account_id": account["id"],
                        "category_id": category_id,
                        "status": "confirmed",
                        "source": "manual",
                        "splits": [],
                    }
                )
                transaction = await transaction_service.create_transaction(session, context, data)
                insert_finished.set()
                return transaction

        confirmation_task = asyncio.create_task(confirm())
        await asyncio.wait_for(snapshot_read.wait(), timeout=5)
        insert_task = asyncio.create_task(insert())
        try:
            await asyncio.wait_for(asyncio.shield(insert_finished.wait()), timeout=0.5)
            inserted_before_confirmation = True
        except TimeoutError:
            inserted_before_confirmation = False
        release_confirmation.set()
        confirmation = await asyncio.wait_for(confirmation_task, timeout=5)
        inserted = await asyncio.wait_for(insert_task, timeout=5)
        return inserted_before_confirmation, confirmation, inserted

    inserted_before_confirmation, confirmation, inserted = asyncio.run(race())
    assert inserted_before_confirmation is False
    assert not isinstance(confirmation, ApiError)
    assert str(inserted.id) not in confirmation.transaction_ids  # type: ignore[union-attr]
    assert inserted.status == "confirmed"


def test_confirmation_rolls_back_all_changes_on_internal_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _bootstrap(client, monkeypatch)
    account = _account(client, headers)
    transaction = _transaction(client, headers, account_id=str(account["id"]), amount="10")
    preview = _preview(client, headers, account, statement_balance="110").json()
    original_record_audit = reconciliation_service.record_audit

    async def fail_transaction_audit(*args: object, **kwargs: object) -> object:
        if kwargs.get("entity_type") == "transaction":
            raise RuntimeError("injected reconciliation failure")
        return await original_record_audit(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reconciliation_service, "record_audit", fail_transaction_audit)
    response = client.post(
        f"/api/v1/accounts/{account['id']}/reconciliation/confirm",
        headers=headers,
        json=_confirm_payload(account, preview),
    )
    assert response.status_code == 500

    async def state() -> tuple[str, int, int, int]:
        async with AsyncSessionFactory() as session:
            current_transaction = await session.get(
                FinancialTransaction, uuid.UUID(str(transaction["id"]))
            )
            current_account = await session.get(Account, uuid.UUID(str(account["id"])))
            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AccountReconciliation)
                    .where(AccountReconciliation.account_id == uuid.UUID(str(account["id"])))
                )
                or 0
            )
            assert current_transaction is not None and current_account is not None
            return (
                current_transaction.status,
                current_transaction.version,
                current_account.version,
                count,
            )

    status, version, account_version, reconciliation_count = asyncio.run(state())
    assert status == "confirmed"
    assert version == transaction["version"]
    assert account_version == account["version"]
    assert reconciliation_count == 0
