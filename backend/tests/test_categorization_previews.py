import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select, update

from app.core.config import settings
from app.db.models.categorization_previews import CategorizationPreview, CategorizationPreviewItem
from app.db.models.transactions import FinancialTransaction, TransactionSplit
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory, engine
from app.dependencies.context import RequestContext
from app.schemas.categorization_previews import (
    CategorizationPreviewCreate,
    CategorizationPreviewFilterSelection,
    CategorizationPreviewIdsSelection,
)
from app.schemas.transactions import SplitInput, TransactionUpdate
from app.services import categorization_previews as preview_service
from app.services import categorization_rules as rule_service
from app.services import transactions as transaction_service
from app.services.financial_period_guard import get_or_create_control

PASSWORD = "correct horse battery staple"
PREVIEWS = "/api/v1/categorization-previews"


@pytest.fixture(autouse=True)
def _configure_preview_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _register(client: TestClient, label: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"preview-{uuid.uuid4()}@example.com",
            "display_name": f"{label} Owner",
            "password": PASSWORD,
            "workspace_name": f"{label} Workspace",
            "base_currency": "RUB",
            "timezone": "Asia/Yekaterinburg",
        },
    )
    assert response.status_code == 201, response.text
    identity = response.json()
    return identity, {
        "Authorization": f"Bearer {identity['access_token']}",
        "X-Workspace-ID": identity["workspace"]["id"],
    }


def _account(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={
            "name": name,
            "account_type": "debit_card",
            "currency": "RUB",
            "opening_balance": "100000",
            "opening_balance_at": "2026-07-01T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _category(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    category_type: str = "expense",
) -> dict:
    response = client.post(
        "/api/v1/categories",
        headers=headers,
        json={"name": name, "category_type": category_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _payee(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post("/api/v1/payees", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _rule(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    category_id: str,
    priority: int = 100,
    **matchers: object,
) -> dict:
    response = client.post(
        "/api/v1/categorization-rules",
        headers=headers,
        json={"name": name, "priority": priority, "category_id": category_id, **matchers},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_transaction(
    identity: dict,
    account_id: str,
    *,
    counterparty: str | None = None,
    description: str | None = None,
    payee_id: str | None = None,
    transaction_type: str = "expense",
    status: str = "confirmed",
    category_id: str | None = None,
    target_account_id: str | None = None,
    occurred_at: datetime | None = None,
    source: str = "import",
) -> str:
    async with AsyncSessionFactory() as session:
        transaction = FinancialTransaction(
            workspace_id=uuid.UUID(identity["workspace"]["id"]),
            occurred_at=occurred_at or datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            transaction_type=transaction_type,
            amount=Decimal("1250.2500"),
            currency="RUB",
            account_id=uuid.UUID(account_id),
            target_account_id=uuid.UUID(target_account_id) if target_account_id else None,
            category_id=uuid.UUID(category_id) if category_id else None,
            payee_id=uuid.UUID(payee_id) if payee_id else None,
            counterparty=counterparty,
            description=description,
            status=status,
            source=source,
            created_by=uuid.UUID(identity["user"]["id"]),
            updated_by=uuid.UUID(identity["user"]["id"]),
        )
        session.add(transaction)
        await session.commit()
        await session.refresh(transaction)
        return str(transaction.id)


async def _add_split(transaction_id: str, category_id: str) -> None:
    async with AsyncSessionFactory() as session:
        session.add(
            TransactionSplit(
                transaction_id=uuid.UUID(transaction_id),
                category_id=uuid.UUID(category_id),
                amount=Decimal("1250.2500"),
            )
        )
        await session.commit()


async def _set_role(identity: dict, role: str) -> None:
    async with AsyncSessionFactory() as session:
        member = await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == uuid.UUID(identity["workspace"]["id"]),
                WorkspaceMember.user_id == uuid.UUID(identity["user"]["id"]),
            )
        )
        assert member is not None
        member.role = role
        await session.commit()


async def _close_through(identity: dict, closed_through: date) -> None:
    async with AsyncSessionFactory() as session:
        control = await get_or_create_control(
            session,
            uuid.UUID(identity["workspace"]["id"]),
            for_update=True,
        )
        control.closed_through = closed_through
        await session.commit()


async def _expire(preview_id: str) -> None:
    async with AsyncSessionFactory() as session:
        await session.execute(
            update(CategorizationPreview)
            .where(CategorizationPreview.id == uuid.UUID(preview_id))
            .values(
                created_at=datetime.now(UTC) - timedelta(hours=48),
                expires_at=datetime.now(UTC) - timedelta(hours=24),
            )
        )
        await session.commit()


async def _context_for(identity: dict, session) -> RequestContext:
    user = await session.get(User, uuid.UUID(identity["user"]["id"]))
    workspace = await session.get(Workspace, uuid.UUID(identity["workspace"]["id"]))
    assert user is not None
    assert workspace is not None
    return RequestContext(
        user=user,
        workspace=workspace,
        role="owner",
        request_id=str(uuid.uuid4()),
    )


async def _persisted_items(preview_id: str) -> list[CategorizationPreviewItem]:
    async with AsyncSessionFactory() as session:
        return list(
            (
                await session.scalars(
                    select(CategorizationPreviewItem)
                    .where(CategorizationPreviewItem.preview_id == uuid.UUID(preview_id))
                    .order_by(CategorizationPreviewItem.sequence)
                )
            ).all()
        )


def _create(client: TestClient, headers: dict[str, str], selection: dict) -> dict:
    response = client.post(PREVIEWS, headers=headers, json={"selection": selection})
    assert response.status_code == 201, response.text
    return response.json()


def _all_items(client: TestClient, headers: dict[str, str], preview_id: str) -> list[dict]:
    collected: list[dict] = []
    offset = 0
    while True:
        page = client.get(
            f"{PREVIEWS}/{preview_id}/items?limit=200&offset={offset}",
            headers=headers,
        )
        assert page.status_code == 200, page.text
        payload = page.json()
        collected.extend(payload["items"])
        offset += 200
        if offset >= payload["page"]["total"]:
            return collected


def test_explicit_ids_preview_persists_ordered_items_and_summary(client: TestClient) -> None:
    identity, headers = _register(client, "Preview ids")
    account = _account(client, headers, "Preview card")
    category = _category(client, headers, "Preview target")
    _rule(
        client,
        headers,
        name="Coffee rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="coffee",
    )
    matched_id = asyncio.run(_insert_transaction(identity, account["id"], counterparty="COFFEE 1"))
    unmatched_id = asyncio.run(_insert_transaction(identity, account["id"], counterparty="Other"))
    missing_id = str(uuid.uuid4())

    created = _create(
        client,
        headers,
        {"mode": "ids", "transaction_ids": [unmatched_id, missing_id, matched_id]},
    )
    assert created["selection_mode"] == "ids"
    assert created["rule_set_version"] >= 1
    assert created["summary"] == {
        "selected": 3,
        "matched": 1,
        "no_match": 1,
        "transfer": 0,
        "already_categorized": 0,
        "split": 0,
        "reconciled": 0,
        "closed_period": 0,
        "not_found": 1,
    }
    expires = datetime.fromisoformat(created["expires_at"])
    assert (
        timedelta(hours=23)
        < expires - datetime.fromisoformat(created["created_at"])
        <= (timedelta(hours=24))
    )

    items = _all_items(client, headers, created["id"])
    # Caller order is preserved for explicit identifiers.
    assert [item["transaction_id"] for item in items] == [unmatched_id, missing_id, matched_id]
    assert [item["sequence"] for item in items] == [0, 1, 2]
    assert [item["status"] for item in items] == ["no_match", "not_found", "matched"]

    not_found_item = items[1]
    assert not_found_item["transaction"] is None
    assert not_found_item["transaction_version"] is None

    matched_item = items[2]
    assert matched_item["rule_name"] == "Coffee rule"
    assert matched_item["rule_version"] == 1
    assert matched_item["category_id"] == category["id"]
    assert matched_item["category_name"] == "Preview target"
    assert matched_item["category_version"] == 1
    assert matched_item["transaction_version"] == 1
    snapshot = matched_item["transaction"]
    assert snapshot["transaction_id"] == matched_id
    assert snapshot["counterparty"] == "COFFEE 1"
    assert snapshot["currency"] == "RUB"
    assert snapshot["status"] == "confirmed"
    assert snapshot["source"] == "import"


def test_filter_preview_selects_uncategorized_transactions_in_canonical_order(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Preview filter")
    account = _account(client, headers, "Filter card")
    other_account = _account(client, headers, "Filter other")
    category = _category(client, headers, "Filter target")
    _rule(
        client,
        headers,
        name="Filter rule",
        category_id=category["id"],
        priority=5,
        counterparty_contains="filter shop",
    )
    older = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Filter Shop old",
            occurred_at=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        )
    )
    newer = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Filter Shop new",
            occurred_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        )
    )
    # Already categorized: filter mode never proposes overwriting an existing category.
    asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Filter Shop categorized",
            category_id=category["id"],
            occurred_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        )
    )
    # Different account: excluded by the account filter.
    asyncio.run(
        _insert_transaction(
            identity,
            other_account["id"],
            counterparty="Filter Shop elsewhere",
            occurred_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
        )
    )

    created = _create(client, headers, {"mode": "filter", "account_id": account["id"]})
    assert created["selection_mode"] == "filter"
    assert created["summary"]["selected"] == 2
    assert created["summary"]["matched"] == 2
    assert created["summary"]["already_categorized"] == 0

    items = _all_items(client, headers, created["id"])
    # Canonical transaction ordering: occurred_at DESC, id DESC.
    assert [item["transaction_id"] for item in items] == [newer, older]


def test_item_pagination_is_stable_and_uses_persisted_sequence(client: TestClient) -> None:
    identity, headers = _register(client, "Preview paging")
    account = _account(client, headers, "Paging card")
    ids = [
        asyncio.run(
            _insert_transaction(
                identity,
                account["id"],
                counterparty=f"Paging {index}",
                occurred_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC) + timedelta(days=index),
            )
        )
        for index in range(5)
    ]
    created = _create(client, headers, {"mode": "ids", "transaction_ids": ids})

    first = client.get(f"{PREVIEWS}/{created['id']}/items?limit=2&offset=0", headers=headers)
    second = client.get(f"{PREVIEWS}/{created['id']}/items?limit=2&offset=2", headers=headers)
    third = client.get(f"{PREVIEWS}/{created['id']}/items?limit=2&offset=4", headers=headers)
    assert first.json()["page"] == {"limit": 2, "offset": 0, "total": 5}
    assert [item["sequence"] for item in first.json()["items"]] == [0, 1]
    assert [item["sequence"] for item in second.json()["items"]] == [2, 3]
    assert [item["sequence"] for item in third.json()["items"]] == [4]

    # Moving a transaction in live canonical order must not reshuffle persisted paging.
    async def move_first_transaction() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(FinancialTransaction)
                .where(FinancialTransaction.id == uuid.UUID(ids[0]))
                .values(occurred_at=datetime(2027, 1, 1, 12, 0, tzinfo=UTC))
            )
            await session.commit()

    asyncio.run(move_first_transaction())
    repeated = client.get(f"{PREVIEWS}/{created['id']}/items?limit=2&offset=0", headers=headers)
    assert [item["transaction_id"] for item in repeated.json()["items"]] == ids[:2]

    over_limit = client.get(f"{PREVIEWS}/{created['id']}/items?limit=500", headers=headers)
    assert over_limit.status_code == 422


def test_selection_validation_rejects_empty_duplicate_and_oversized_id_lists(
    client: TestClient,
) -> None:
    _identity, headers = _register(client, "Preview validation")
    duplicate = str(uuid.uuid4())

    empty = client.post(
        PREVIEWS,
        headers=headers,
        json={"selection": {"mode": "ids", "transaction_ids": []}},
    )
    assert empty.status_code == 422

    duplicated = client.post(
        PREVIEWS,
        headers=headers,
        json={"selection": {"mode": "ids", "transaction_ids": [duplicate, duplicate]}},
    )
    assert duplicated.status_code == 422

    oversized = client.post(
        PREVIEWS,
        headers=headers,
        json={
            "selection": {
                "mode": "ids",
                "transaction_ids": [str(uuid.uuid4()) for _ in range(501)],
            }
        },
    )
    assert oversized.status_code == 422

    at_limit = client.post(
        PREVIEWS,
        headers=headers,
        json={
            "selection": {
                "mode": "ids",
                "transaction_ids": [str(uuid.uuid4()) for _ in range(500)],
            }
        },
    )
    assert at_limit.status_code == 201, at_limit.text
    assert at_limit.json()["summary"]["selected"] == 500
    assert at_limit.json()["summary"]["not_found"] == 500


def test_filter_overflow_is_rejected_with_the_maximum_in_details(client: TestClient) -> None:
    identity, headers = _register(client, "Preview overflow")
    account = _account(client, headers, "Overflow card")
    workspace_id = uuid.UUID(identity["workspace"]["id"])
    user_id = uuid.UUID(identity["user"]["id"])
    limit = preview_service.MAX_FILTER_CANDIDATES

    async def seed(count: int) -> None:
        base = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        rows = [
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace_id,
                "occurred_at": base + timedelta(seconds=index),
                "transaction_type": "expense",
                "amount": Decimal("10.0000"),
                "currency": "RUB",
                "account_id": uuid.UUID(account["id"]),
                "counterparty": f"Overflow {index}",
                "status": "confirmed",
                "source": "import",
                "version": 1,
                "created_by": user_id,
                "updated_by": user_id,
            }
            for index in range(count)
        ]
        async with AsyncSessionFactory() as session:
            for start in range(0, len(rows), 1000):
                await session.execute(insert(FinancialTransaction), rows[start : start + 1000])
            await session.commit()

    asyncio.run(seed(limit + 1))

    rejected = client.post(PREVIEWS, headers=headers, json={"selection": {"mode": "filter"}})
    assert rejected.status_code == 422, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "CATEGORIZATION_PREVIEW_LIMIT_EXCEEDED"
    assert error["details"]["maximum"] == limit

    # Narrowing to exactly the maximum is accepted.
    boundary = datetime(2026, 8, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=limit - 1)
    accepted = client.post(
        PREVIEWS,
        headers=headers,
        json={"selection": {"mode": "filter", "occurred_to": boundary.isoformat()}},
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["summary"]["selected"] == limit
    assert accepted.json()["summary"]["no_match"] == limit


def test_every_classification_status_and_precedence_is_persisted(client: TestClient) -> None:
    identity, headers = _register(client, "Preview statuses")
    account = _account(client, headers, "Status card")
    target_account = _account(client, headers, "Status target")
    category = _category(client, headers, "Status target category")
    _rule(
        client,
        headers,
        name="Status rule",
        category_id=category["id"],
        priority=1,
        counterparty_contains="status shop",
    )

    matched = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Status Shop match")
    )
    no_match = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Unrelated statement")
    )
    transfer = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Status Shop transfer",
            transaction_type="transfer",
            target_account_id=target_account["id"],
        )
    )
    already = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Status Shop categorized",
            category_id=category["id"],
        )
    )
    split = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Status Shop split")
    )
    asyncio.run(_add_split(split, category["id"]))
    reconciled = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Status Shop reconciled",
            status="reconciled",
        )
    )
    closed = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Status Shop closed",
            occurred_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        )
    )
    # Precedence probes: each carries a later-precedence condition as well.
    transfer_and_split = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Status Shop transfer split",
            transaction_type="transfer",
            target_account_id=target_account["id"],
            status="reconciled",
        )
    )
    already_and_reconciled = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Status Shop categorized reconciled",
            category_id=category["id"],
            status="reconciled",
        )
    )
    split_and_closed = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Status Shop split closed",
            occurred_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
        )
    )
    asyncio.run(_add_split(split_and_closed, category["id"]))
    reconciled_and_closed = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Status Shop reconciled closed",
            status="reconciled",
            occurred_at=datetime(2026, 6, 17, 12, 0, tzinfo=UTC),
        )
    )
    asyncio.run(_close_through(identity, date(2026, 6, 30)))
    missing = str(uuid.uuid4())

    ordered = [
        missing,
        transfer,
        already,
        split,
        reconciled,
        closed,
        matched,
        no_match,
        transfer_and_split,
        already_and_reconciled,
        split_and_closed,
        reconciled_and_closed,
    ]
    created = _create(client, headers, {"mode": "ids", "transaction_ids": ordered})
    items = _all_items(client, headers, created["id"])
    assert [item["status"] for item in items] == [
        "not_found",
        "transfer",
        "already_categorized",
        "split",
        "reconciled",
        "closed_period",
        "matched",
        "no_match",
        # transfer wins over reconciled
        "transfer",
        # already_categorized wins over reconciled
        "already_categorized",
        # split wins over closed_period
        "split",
        # reconciled wins over closed_period
        "reconciled",
    ]
    assert created["summary"] == {
        "selected": 12,
        "matched": 1,
        "no_match": 1,
        "transfer": 2,
        "already_categorized": 2,
        "split": 2,
        "reconciled": 2,
        "closed_period": 1,
        "not_found": 1,
    }
    persisted = asyncio.run(_persisted_items(created["id"]))
    assert len(persisted) == created["summary"]["selected"]
    assert sum(1 for item in persisted if item.status == "matched") == 1


def test_matcher_parity_between_single_preview_and_bulk_preview(client: TestClient) -> None:
    identity, headers = _register(client, "Preview parity")
    account = _account(client, headers, "Parity card")
    other_account = _account(client, headers, "Parity other")
    archived_category = _category(client, headers, "Parity archived")
    income_category = _category(client, headers, "Parity income", category_type="income")
    winner_category = _category(client, headers, "Parity winner")
    payee = _payee(client, headers, "Parity payee")

    # Highest priority but its target category is archived: skipped, evaluation continues.
    _rule(
        client,
        headers,
        name="Archived target",
        category_id=archived_category["id"],
        priority=1,
        counterparty_contains="parity shop",
    )
    # Next priority but the target category type is incompatible with an expense: also skipped.
    _rule(
        client,
        headers,
        name="Incompatible target",
        category_id=income_category["id"],
        priority=2,
        counterparty_contains="parity shop",
    )
    # AND semantics: every filled matcher must hold.
    _rule(
        client,
        headers,
        name="Wrong account",
        category_id=winner_category["id"],
        priority=3,
        account_id=other_account["id"],
        counterparty_contains="parity shop",
    )
    winner = _rule(
        client,
        headers,
        name="Parity winner rule",
        category_id=winner_category["id"],
        priority=4,
        account_id=account["id"],
        payee_id=payee["id"],
        counterparty_contains="parity shop",
        description_contains="subscription",
    )

    async def archive_category() -> None:
        from app.db.models.categories import Category

        async with AsyncSessionFactory() as session:
            await session.execute(
                update(Category)
                .where(Category.id == uuid.UUID(archived_category["id"]))
                .values(is_archived=True)
            )
            await session.commit()

    asyncio.run(archive_category())

    transaction_id = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            # Fullwidth characters plus padded whitespace exercise NFKC + collapse + casefold.
            counterparty="  Ｐａｒｉｔｙ   SHOP 9  ",  # noqa: RUF001
            description="Monthly SUBSCRIPTION",
            payee_id=payee["id"],
        )
    )

    single = client.post(
        "/api/v1/categorization-rules/preview",
        headers=headers,
        json={"transaction_id": transaction_id},
    )
    assert single.status_code == 200, single.text
    assert single.json()["matched"] is True
    assert single.json()["rule"]["id"] == winner["id"]
    assert single.json()["category"]["id"] == winner_category["id"]

    bulk = _create(client, headers, {"mode": "ids", "transaction_ids": [transaction_id]})
    item = _all_items(client, headers, bulk["id"])[0]
    assert item["status"] == "matched"
    assert item["rule_id"] == winner["id"]
    assert item["category_id"] == winner_category["id"]

    # A rule keyed on a payee never matches through counterparty text alone.
    without_payee = asyncio.run(
        _insert_transaction(
            identity,
            account["id"],
            counterparty="Parity Shop 9",
            description="Monthly subscription",
        )
    )
    payee_bulk = _create(client, headers, {"mode": "ids", "transaction_ids": [without_payee]})
    assert _all_items(client, headers, payee_bulk["id"])[0]["status"] == "no_match"


def test_preview_evaluation_uses_bounded_rule_category_and_split_queries(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Preview queries")
    account = _account(client, headers, "Query card")
    categories = [_category(client, headers, f"Query target {index}") for index in range(5)]
    for index, category in enumerate(categories):
        _rule(
            client,
            headers,
            name=f"Query rule {index}",
            category_id=category["id"],
            priority=index + 1,
            counterparty_contains=f"query shop {index}",
        )
    ids = [
        asyncio.run(
            _insert_transaction(
                identity,
                account["id"],
                counterparty=f"Query Shop {index % 5} statement",
                occurred_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC) + timedelta(minutes=index),
            )
        )
        for index in range(100)
    ]

    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement.lower())

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        created = _create(client, headers, {"mode": "ids", "transaction_ids": ids})
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)

    assert created["summary"]["selected"] == 100
    assert created["summary"]["matched"] == 100
    rule_queries = [item for item in statements if "from categorization_rules" in item]
    category_queries = [
        item for item in statements if "from categories" in item and "categorization" not in item
    ]
    split_queries = [item for item in statements if "from transaction_splits" in item]
    transaction_queries = [item for item in statements if "from transactions" in item]
    # 100 transactions must not cause per-transaction rule, category or split lookups. Transaction
    # state and correlated split existence are deliberately folded into the same statement.
    assert len(rule_queries) <= 2, rule_queries
    assert len(category_queries) <= 2, category_queries
    assert len(transaction_queries) == 1, transaction_queries
    assert len(split_queries) == 1, split_queries


def test_workspace_isolation_roles_and_creator_do_not_gate_reads(client: TestClient) -> None:
    owner_identity, owner_headers = _register(client, "Preview owner")
    account = _account(client, owner_headers, "Isolation card")
    foreign_identity, foreign_headers = _register(client, "Preview foreign")
    foreign_account = _account(client, foreign_headers, "Foreign card")
    foreign_transaction = asyncio.run(
        _insert_transaction(foreign_identity, foreign_account["id"], counterparty="Foreign")
    )
    own_transaction = asyncio.run(
        _insert_transaction(owner_identity, account["id"], counterparty="Own")
    )

    # A foreign identifier is indistinguishable from a nonexistent one.
    created = _create(
        client,
        owner_headers,
        {"mode": "ids", "transaction_ids": [own_transaction, foreign_transaction]},
    )
    items = _all_items(client, owner_headers, created["id"])
    assert items[1]["status"] == "not_found"
    assert items[1]["transaction"] is None
    assert created["summary"]["not_found"] == 1

    # Another workspace cannot see the preview at all.
    foreign_read = client.get(f"{PREVIEWS}/{created['id']}", headers=foreign_headers)
    assert foreign_read.status_code == 404
    assert foreign_read.json()["error"]["code"] == "CATEGORIZATION_PREVIEW_NOT_FOUND"
    foreign_items = client.get(f"{PREVIEWS}/{created['id']}/items", headers=foreign_headers)
    assert foreign_items.status_code == 404

    # A second member of the same workspace reads a preview they did not create.
    second = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"preview-member-{uuid.uuid4()}@example.com",
            "display_name": "Preview member",
            "password": PASSWORD,
            "workspace_name": "Discarded workspace",
            "base_currency": "RUB",
            "timezone": "Asia/Yekaterinburg",
        },
    )
    assert second.status_code == 201, second.text
    member_identity = second.json()

    async def join_workspace(role: str) -> None:
        async with AsyncSessionFactory() as session:
            session.add(
                WorkspaceMember(
                    workspace_id=uuid.UUID(owner_identity["workspace"]["id"]),
                    user_id=uuid.UUID(member_identity["user"]["id"]),
                    role=role,
                )
            )
            await session.commit()

    asyncio.run(join_workspace("viewer"))
    member_headers = {
        "Authorization": f"Bearer {member_identity['access_token']}",
        "X-Workspace-ID": owner_identity["workspace"]["id"],
    }
    viewer_read = client.get(f"{PREVIEWS}/{created['id']}", headers=member_headers)
    assert viewer_read.status_code == 200, viewer_read.text
    assert viewer_read.json()["created_by"] == owner_identity["user"]["id"]

    # A viewer may also create a preview: preview never mutates financial data.
    viewer_created = client.post(
        PREVIEWS,
        headers=member_headers,
        json={"selection": {"mode": "ids", "transaction_ids": [own_transaction]}},
    )
    assert viewer_created.status_code == 201, viewer_created.text
    assert viewer_created.json()["created_by"] == member_identity["user"]["id"]
    # And the owner reads the viewer's preview.
    assert (
        client.get(f"{PREVIEWS}/{viewer_created.json()['id']}", headers=owner_headers).status_code
        == 200
    )

    for role in ("editor", "owner"):
        member_in_workspace = {
            "workspace": owner_identity["workspace"],
            "user": member_identity["user"],
        }
        asyncio.run(_set_role(member_in_workspace, role))
        assert client.get(f"{PREVIEWS}/{created['id']}", headers=member_headers).status_code == 200


def test_expired_preview_reports_gone_while_foreign_workspace_stays_not_found(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Preview expiry")
    _foreign_identity, foreign_headers = _register(client, "Preview expiry foreign")
    account = _account(client, headers, "Expiry card")
    transaction_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Expiring")
    )
    created = _create(client, headers, {"mode": "ids", "transaction_ids": [transaction_id]})

    assert client.get(f"{PREVIEWS}/{created['id']}", headers=headers).status_code == 200
    asyncio.run(_expire(created["id"]))

    expired = client.get(f"{PREVIEWS}/{created['id']}", headers=headers)
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "CATEGORIZATION_PREVIEW_EXPIRED"
    expired_items = client.get(f"{PREVIEWS}/{created['id']}/items", headers=headers)
    assert expired_items.status_code == 410

    # Expiry never leaks existence across workspaces.
    assert client.get(f"{PREVIEWS}/{created['id']}", headers=foreign_headers).status_code == 404

    async def prune() -> int:
        from app.repositories import categorization_previews as preview_repository

        async with AsyncSessionFactory() as session:
            removed = await preview_repository.delete_expired(
                session,
                uuid.UUID(identity["workspace"]["id"]),
                datetime.now(UTC),
            )
            await session.commit()
            return removed

    assert asyncio.run(prune()) == 1
    assert asyncio.run(_persisted_items(created["id"])) == []


def test_persisted_items_are_immutable_after_rule_and_transaction_changes(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Preview immutable")
    account = _account(client, headers, "Immutable card")
    category = _category(client, headers, "Immutable target")
    replacement = _category(client, headers, "Immutable replacement")
    rule = _rule(
        client,
        headers,
        name="Immutable rule",
        category_id=category["id"],
        priority=10,
        counterparty_contains="immutable shop",
    )
    transaction_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Immutable Shop 1")
    )
    created = _create(client, headers, {"mode": "ids", "transaction_ids": [transaction_id]})
    captured_revision = created["rule_set_version"]
    before = _all_items(client, headers, created["id"])[0]
    assert before["status"] == "matched"
    assert before["category_id"] == category["id"]
    assert before["transaction_version"] == 1

    # Retarget the rule and archive it, then change the transaction.
    retarget = client.patch(
        f"/api/v1/categorization-rules/{rule['id']}",
        headers=headers,
        json={"version": rule["version"], "category_id": replacement["id"]},
    )
    assert retarget.status_code == 200, retarget.text
    archived = client.delete(
        f"/api/v1/categorization-rules/{rule['id']}?version={retarget.json()['version']}",
        headers=headers,
    )
    assert archived.status_code == 200, archived.text

    async def bump_transaction() -> None:
        async with AsyncSessionFactory() as session:
            await session.execute(
                update(FinancialTransaction)
                .where(FinancialTransaction.id == uuid.UUID(transaction_id))
                .values(version=7, counterparty="Renamed counterparty")
            )
            await session.commit()

    asyncio.run(bump_transaction())

    after_header = client.get(f"{PREVIEWS}/{created['id']}", headers=headers)
    assert after_header.status_code == 200
    # The captured revision and the persisted proposal are historical facts.
    assert after_header.json()["rule_set_version"] == captured_revision
    after = _all_items(client, headers, created["id"])[0]
    assert after == before


def test_candidate_version_and_split_state_share_one_statement_snapshot(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Preview transaction split race")
    account = _account(client, headers, "Atomic snapshot card")
    category = _category(client, headers, "Atomic split category")
    transaction_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Atomic snapshot shop")
    )

    snapshot_read = asyncio.Event()
    release_preview = asyncio.Event()
    original_filtered_candidates = preview_service.repository.filtered_candidates

    async def paused_filtered_candidates(*args: object, **kwargs: object):
        candidates = await original_filtered_candidates(*args, **kwargs)  # type: ignore[arg-type]
        snapshot_read.set()
        await release_preview.wait()
        return candidates

    monkeypatch.setattr(
        preview_service.repository,
        "filtered_candidates",
        paused_filtered_candidates,
    )

    async def race() -> str:
        async with AsyncSessionFactory() as preview_session:
            preview_task = asyncio.create_task(
                preview_service.create_preview(
                    preview_session,
                    await _context_for(identity, preview_session),
                    CategorizationPreviewCreate(
                        selection=CategorizationPreviewFilterSelection(
                            mode="filter",
                            account_id=uuid.UUID(account["id"]),
                        )
                    ),
                )
            )
            await asyncio.wait_for(snapshot_read.wait(), timeout=5)
            try:
                async with AsyncSessionFactory() as mutation_session:
                    await transaction_service.update_transaction(
                        mutation_session,
                        await _context_for(identity, mutation_session),
                        uuid.UUID(transaction_id),
                        TransactionUpdate(
                            version=1,
                            splits=[
                                SplitInput(
                                    category_id=uuid.UUID(category["id"]),
                                    amount=Decimal("1250.2500"),
                                )
                            ],
                        ),
                    )
            finally:
                release_preview.set()
            preview = await asyncio.wait_for(preview_task, timeout=5)
            return str(preview.id)

    preview_id = asyncio.run(race())
    item = asyncio.run(_persisted_items(preview_id))[0]
    # The mutation committed after the candidate statement. The preview may therefore preserve v1,
    # but it must never combine that version with v2 split existence.
    assert item.transaction_version == 1
    assert item.transaction_snapshot is not None
    assert item.transaction_snapshot["version"] == 1
    assert item.status == "no_match"

    async def current_state() -> tuple[int, int]:
        async with AsyncSessionFactory() as session:
            transaction = await session.get(FinancialTransaction, uuid.UUID(transaction_id))
            assert transaction is not None
            splits = list(
                (
                    await session.scalars(
                        select(TransactionSplit).where(
                            TransactionSplit.transaction_id == uuid.UUID(transaction_id)
                        )
                    )
                ).all()
            )
            return transaction.version, len(splits)

    assert asyncio.run(current_state()) == (2, 1)


def test_filter_soft_delete_after_candidate_snapshot_never_becomes_not_found(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Preview filter deletion race")
    account = _account(client, headers, "Deletion snapshot card")
    transaction_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Deletion snapshot shop")
    )

    snapshot_read = asyncio.Event()
    release_preview = asyncio.Event()
    original_filtered_candidates = preview_service.repository.filtered_candidates

    async def paused_filtered_candidates(*args: object, **kwargs: object):
        candidates = await original_filtered_candidates(*args, **kwargs)  # type: ignore[arg-type]
        snapshot_read.set()
        await release_preview.wait()
        return candidates

    monkeypatch.setattr(
        preview_service.repository,
        "filtered_candidates",
        paused_filtered_candidates,
    )

    async def race() -> str:
        async with AsyncSessionFactory() as preview_session:
            preview_task = asyncio.create_task(
                preview_service.create_preview(
                    preview_session,
                    await _context_for(identity, preview_session),
                    CategorizationPreviewCreate(
                        selection=CategorizationPreviewFilterSelection(
                            mode="filter",
                            account_id=uuid.UUID(account["id"]),
                        )
                    ),
                )
            )
            await asyncio.wait_for(snapshot_read.wait(), timeout=5)
            try:
                async with AsyncSessionFactory() as mutation_session:
                    await transaction_service.delete_transaction(
                        mutation_session,
                        await _context_for(identity, mutation_session),
                        uuid.UUID(transaction_id),
                        version=1,
                    )
            finally:
                release_preview.set()
            preview = await asyncio.wait_for(preview_task, timeout=5)
            return str(preview.id)

    preview_id = asyncio.run(race())
    item = asyncio.run(_persisted_items(preview_id))[0]
    # Filter membership and row state linearize together. A delete after that point cannot turn a
    # real filter candidate into the explicit-ID-only ``not_found`` classification.
    assert item.transaction_id == uuid.UUID(transaction_id)
    assert item.transaction_version == 1
    assert item.status == "no_match"

    async def deleted_state() -> tuple[int, bool]:
        async with AsyncSessionFactory() as session:
            transaction = await session.get(FinancialTransaction, uuid.UUID(transaction_id))
            assert transaction is not None
            return transaction.version, transaction.deleted_at is not None

    assert asyncio.run(deleted_state()) == (2, True)


def test_rule_mutation_waits_for_a_preview_holding_the_shared_rule_set_lock(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Preview barrier")
    account = _account(client, headers, "Barrier card")
    category = _category(client, headers, "Barrier target")
    intruder_category = _category(client, headers, "Barrier intruder")
    selected = _rule(
        client,
        headers,
        name="Barrier rule",
        category_id=category["id"],
        priority=50,
        counterparty_contains="barrier shop",
    )
    transaction_id = asyncio.run(
        _insert_transaction(identity, account["id"], counterparty="Barrier Shop statement")
    )

    lock_held = asyncio.Event()
    allow_finish = asyncio.Event()
    original_prepare = preview_service.prepare_rule_set

    async def paused_prepare(session, workspace_id, *, refresh: bool = False):
        prepared = await original_prepare(session, workspace_id, refresh=refresh)
        lock_held.set()
        await allow_finish.wait()
        return prepared

    monkeypatch.setattr(preview_service, "prepare_rule_set", paused_prepare)

    async def run_barrier() -> tuple[str, bool, int]:
        async with AsyncSessionFactory() as preview_session:
            preview_task = asyncio.create_task(
                preview_service.create_preview(
                    preview_session,
                    await _context_for(identity, preview_session),
                    CategorizationPreviewCreate(
                        selection=CategorizationPreviewIdsSelection(
                            mode="ids",
                            transaction_ids=[uuid.UUID(transaction_id)],
                        )
                    ),
                )
            )
            await lock_held.wait()
            async with AsyncSessionFactory() as mutation_session:
                from app.schemas.categorization_rules import CategorizationRuleCreate

                mutation_task = asyncio.create_task(
                    rule_service.create_rule(
                        mutation_session,
                        await _context_for(identity, mutation_session),
                        CategorizationRuleCreate(
                            name="Barrier intruder",
                            priority=1,
                            category_id=uuid.UUID(intruder_category["id"]),
                            counterparty_contains="barrier shop",
                        ),
                    )
                )
                # The exclusive rule-set gate must park the mutation while preview builds.
                await asyncio.sleep(0.5)
                blocked = not mutation_task.done()
                allow_finish.set()
                preview = await preview_task
                await mutation_task
            return str(preview.id), blocked, preview.rule_set_version

    preview_id, blocked, revision = asyncio.run(run_barrier())
    assert blocked is True

    items = _all_items(client, headers, preview_id)
    assert len(items) == 1
    # The preview used the rule set that was current when it took the shared lock.
    assert items[0]["rule_id"] == selected["id"]
    assert items[0]["category_id"] == category["id"]

    header = client.get(f"{PREVIEWS}/{preview_id}", headers=headers)
    assert header.json()["rule_set_version"] == revision

    # The rule set has moved on, but the persisted proposal has not been rewritten.
    async def current_revision() -> int:
        from app.db.models.categorization_rule_sets import CategorizationRuleSetControl

        async with AsyncSessionFactory() as session:
            value = await session.scalar(
                select(CategorizationRuleSetControl.version).where(
                    CategorizationRuleSetControl.workspace_id
                    == uuid.UUID(identity["workspace"]["id"])
                )
            )
            return int(value or 0)

    assert asyncio.run(current_revision()) == revision + 1
    assert _all_items(client, headers, preview_id)[0]["rule_id"] == selected["id"]
