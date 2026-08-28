import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.audit import AuditLog
from app.db.models.automations import (
    AutomationRun,
    MonthCloseRevision,
    MonthClosure,
    RecurringRule,
    RecurringRuleExecution,
)
from app.db.models.budgets import BudgetAllocation, BudgetPeriod, BudgetPlanRevision
from app.db.models.categories import Category
from app.db.models.google_sync import SyncOutbox
from app.db.models.transactions import FinancialTransaction, TransactionSplit
from app.db.models.users import User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionFactory, engine, get_forecast_session
from app.services import budget_forecasts, recurrence, recurring_rules
from tests.test_automations import _register
from tests.test_budgets import _account, _budget, _category, _confirm, _prepare, _transaction

ZERO = Decimal("0.0000")


@pytest.fixture(autouse=True)
def _configure_forecast_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "allow_dev_auth_headers", True)


def _rule(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    account_id: str,
    category_id: str | None,
    transaction_type: str,
    amount: str,
    mode: str = "confirmed",
    rrule: str = "FREQ=MONTHLY;BYMONTHDAY=15;BYHOUR=0;BYMINUTE=0",
    target_account_id: str | None = None,
    currency: str = "RUB",
    timezone: str = "UTC",
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/recurring-rules",
        headers=headers,
        json={
            "name": name,
            "rule_type": transaction_type,
            "schedule_rrule": rrule,
            "timezone": timezone,
            "transaction_type": transaction_type,
            "amount": amount,
            "currency": currency,
            "account_id": account_id,
            "target_account_id": target_account_id,
            "category_id": category_id,
            "creation_mode": mode,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _set_rule_state(
    rule_id: str,
    *,
    cursor: datetime | None,
    anchor: datetime | None = None,
    active: bool = True,
    deleted: bool = False,
) -> None:
    async with AsyncSessionFactory() as session:
        rule = await session.get(RecurringRule, uuid.UUID(rule_id))
        assert rule is not None
        rule.next_run_at = cursor
        rule.is_active = active
        rule.deleted_at = datetime.now(UTC) if deleted else None
        if anchor is not None:
            rule.created_at = anchor
        await session.commit()


async def _forecast(
    workspace_id: str,
    period: date,
    currency: str,
    as_of: datetime,
    *,
    include_occurrences: bool = True,
) -> Any:
    manager = asynccontextmanager(get_forecast_session)
    async with manager() as session:
        workspace = await session.get(Workspace, uuid.UUID(workspace_id))
        assert workspace is not None
        return await budget_forecasts.get_forecast(
            session,
            workspace,
            period,
            currency,
            as_of=as_of,
            include_occurrences=include_occurrences,
        )


def _create_budget_references(
    client: TestClient,
    headers: dict[str, str],
    period: str,
    *,
    currency: str = "RUB",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    account = _account(client, headers, currency=currency)
    expense = _category(client, headers, "expense")
    income = _category(client, headers, "income")
    response = _budget(
        client,
        headers,
        period,
        currency,
        planned_income="1000.0000",
        allocations=[{"category_id": expense["id"], "planned_amount": "500.0000"}],
    )
    assert response.status_code == 200, response.text
    return account, expense, income


def test_occurrences_between_preserves_cursor_phase_and_half_open_bounds() -> None:
    anchor = datetime(2026, 7, 30, tzinfo=UTC)
    result = recurrence.occurrences_between(
        "FREQ=DAILY;INTERVAL=2;BYHOUR=0;BYMINUTE=0",
        "UTC",
        first_occurrence=datetime(2026, 8, 1, tzinfo=UTC),
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 8, tzinfo=UTC),
        anchor=anchor,
        limit=20,
    )
    assert result.occurrences == (
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 3, tzinfo=UTC),
        datetime(2026, 8, 5, tzinfo=UTC),
        datetime(2026, 8, 7, tzinfo=UTC),
    )
    assert result.generated_count == 4

    exact_end = recurrence.occurrences_between(
        "FREQ=YEARLY;BYMONTH=8;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0",
        "UTC",
        first_occurrence=datetime(2026, 8, 1, tzinfo=UTC),
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2027, 8, 1, tzinfo=UTC),
        anchor=datetime(2026, 8, 1, tzinfo=UTC),
        limit=2,
    )
    assert exact_end.occurrences == (datetime(2026, 8, 1, tzinfo=UTC),)


@pytest.mark.parametrize(
    ("timezone", "cursor"),
    [
        ("Europe/Berlin", datetime(2026, 3, 29, 1, 30, tzinfo=UTC)),
        ("Europe/Berlin", datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
    ],
)
def test_bounded_expansion_matches_executor_dst_advancement(
    timezone: str, cursor: datetime
) -> None:
    rule = "FREQ=DAILY;BYHOUR=2;BYMINUTE=30"
    expansion = recurrence.occurrences_between(
        rule,
        timezone,
        first_occurrence=cursor,
        start=cursor,
        end=cursor.replace(day=cursor.day + 2),
        anchor=cursor,
        limit=5,
    )
    assert expansion.occurrences[0] == cursor
    assert expansion.occurrences[1] == recurrence.next_occurrence(
        rule, timezone, after=cursor, anchor=cursor
    )


def test_snapshot_dependency_is_repeatable_read_and_read_only() -> None:
    async def inspect() -> tuple[str, str]:
        manager = asynccontextmanager(get_forecast_session)
        async with manager() as session:
            isolation = await session.scalar(text("SHOW transaction_isolation"))
            read_only = await session.scalar(text("SHOW transaction_read_only"))
            return str(isolation), str(read_only)

    assert asyncio.run(inspect()) == ("repeatable read", "on")


def test_forecast_transaction_is_configured_first_and_sessions_are_released() -> None:
    async def inspect_normal_session() -> tuple[str, str]:
        async with AsyncSessionFactory() as session:
            isolation = await session.scalar(text("SHOW transaction_isolation"))
            read_only = await session.scalar(text("SHOW transaction_read_only"))
            return str(isolation), str(read_only)

    assert asyncio.run(inspect_normal_session()) == ("read committed", "off")

    statements: list[str] = []
    checkouts = 0
    checkins = 0

    def record_statement(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement.strip())

    def record_checkout(
        dbapi_connection: object,
        connection_record: object,
        connection_proxy: object,
    ) -> None:
        nonlocal checkouts
        checkouts += 1

    def record_checkin(dbapi_connection: object, connection_record: object) -> None:
        nonlocal checkins
        checkins += 1

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    event.listen(engine.sync_engine.pool, "checkout", record_checkout)
    event.listen(engine.sync_engine.pool, "checkin", record_checkin)
    try:

        async def exercise() -> None:
            manager = asynccontextmanager(get_forecast_session)
            for _ in range(3):
                async with manager() as session:
                    assert await session.scalar(text("SELECT 1")) == 1
            with pytest.raises(RuntimeError, match="forecast service failure"):
                async with manager() as session:
                    assert await session.scalar(text("SELECT 1")) == 1
                    raise RuntimeError("forecast service failure")

        asyncio.run(exercise())
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)
        event.remove(engine.sync_engine.pool, "checkout", record_checkout)
        event.remove(engine.sync_engine.pool, "checkin", record_checkin)

    assert statements[0].upper() == "SET TRANSACTION READ ONLY"
    assert checkouts == 4
    assert checkins == checkouts


def test_current_forecast_modes_advisory_transfer_currency_and_categories(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast core")
    account, expense, income = _create_budget_references(client, headers, "2026-08")
    target = _account(client, headers, name="Transfer target")
    as_of = datetime(2026, 8, 10, tzinfo=UTC)
    cursor = datetime(2026, 8, 15, tzinfo=UTC)
    anchor = datetime(2026, 7, 15, tzinfo=UTC)

    rules = [
        _rule(
            client,
            headers,
            name="Confirmed income",
            account_id=account["id"],
            category_id=income["id"],
            transaction_type="income",
            amount="100.1000",
            mode="confirmed",
        ),
        _rule(
            client,
            headers,
            name="Large exact income",
            account_id=account["id"],
            category_id=income["id"],
            transaction_type="income",
            amount="999999999999.9999",
            mode="confirmed",
        ),
        _rule(
            client,
            headers,
            name="Draft expense",
            account_id=account["id"],
            category_id=expense["id"],
            transaction_type="expense",
            amount="25.0001",
            mode="draft",
        ),
        _rule(
            client,
            headers,
            name="Reminder",
            account_id=account["id"],
            category_id=expense["id"],
            transaction_type="expense",
            amount="7.0000",
            mode="reminder_only",
        ),
        _rule(
            client,
            headers,
            name="Transfer",
            account_id=account["id"],
            target_account_id=target["id"],
            category_id=None,
            transaction_type="transfer",
            amount="50.0000",
        ),
    ]
    for rule in rules:
        asyncio.run(_set_rule_state(rule["id"], cursor=cursor, anchor=anchor))

    paused = _rule(
        client,
        headers,
        name="Paused",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="999.0000",
    )
    deleted = _rule(
        client,
        headers,
        name="Deleted",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="999.0000",
    )
    exhausted = _rule(
        client,
        headers,
        name="Exhausted",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="999.0000",
    )
    asyncio.run(_set_rule_state(paused["id"], cursor=cursor, active=False))
    asyncio.run(_set_rule_state(deleted["id"], cursor=cursor, deleted=True))
    asyncio.run(_set_rule_state(exhausted["id"], cursor=None))

    result = asyncio.run(_forecast(identity["workspace"]["id"], date(2026, 8, 1), "RUB", as_of))
    assert result.period_state == "open_current"
    assert result.forecast.income == Decimal("1000000000100.0999")
    assert result.forecast.expense == Decimal("25.0001")
    assert result.forecast.net_cashflow == Decimal("1000000000075.0998")
    assert result.forecast.scheduled_occurrence_count == 3
    assert result.forecast.mode_breakdown[0].income == Decimal("1000000000100.0999")
    assert result.model_dump(mode="json")["forecast"]["income"] == "1000000000100.0999"
    assert result.advisory.expense == Decimal("7.0000")
    assert result.advisory.occurrence_count == 1
    assert result.informational_transfers.volume == Decimal("50.0000")
    assert result.informational_transfers.occurrence_count == 1
    assert result.category_forecast[0].forecast_expense == Decimal("25.0001")
    assert result.category_forecast[0].projected_remaining == Decimal("474.9999")
    assert {item.state for item in result.occurrences} == {
        "scheduled",
        "advisory",
        "informational_transfer",
    }

    summary = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            as_of,
            include_occurrences=False,
        )
    )
    assert summary.forecast == result.forecast
    assert summary.occurrences == []


def test_overdue_failed_incomplete_block_and_exact_as_of_inclusion(client: TestClient) -> None:
    identity, headers = _register(client, "Forecast blocked")
    account, expense, _ = _create_budget_references(client, headers, "2026-08")
    as_of = datetime(2026, 8, 10, tzinfo=UTC)
    overdue = datetime(2026, 8, 9, tzinfo=UTC)

    no_execution = _rule(
        client,
        headers,
        name="Overdue",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="10.0000",
        rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
    )
    failed = _rule(
        client,
        headers,
        name="Failed",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="20.0000",
        rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
    )
    incomplete = _rule(
        client,
        headers,
        name="Incomplete",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="30.0000",
        rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
    )
    exact = _rule(
        client,
        headers,
        name="Exact as-of",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="0.0001",
        rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0;UNTIL=20260810T000000Z",
    )
    for rule in (no_execution, failed, incomplete):
        asyncio.run(_set_rule_state(rule["id"], cursor=overdue, anchor=overdue))
    asyncio.run(_set_rule_state(exact["id"], cursor=as_of, anchor=as_of))

    async def insert_executions() -> None:
        async with AsyncSessionFactory() as session:
            for payload, status in ((failed, "failed"), (incomplete, "created")):
                run = AutomationRun(
                    workspace_id=uuid.UUID(identity["workspace"]["id"]),
                    automation_type="recurring_rule",
                    trigger_type="test",
                    idempotency_key=f"forecast-{status}-{uuid.uuid4()}",
                    status="failed" if status == "failed" else "started",
                    started_at=overdue,
                    created_at=overdue,
                )
                session.add(run)
                await session.flush()
                session.add(
                    RecurringRuleExecution(
                        rule_id=uuid.UUID(payload["id"]),
                        scheduled_for=overdue,
                        automation_run_id=run.id,
                        status=status,
                        created_at=overdue,
                    )
                )
            await session.commit()

    asyncio.run(insert_executions())
    result = asyncio.run(_forecast(identity["workspace"]["id"], date(2026, 8, 1), "RUB", as_of))
    assert result.forecast.expense == Decimal("0.0001")
    assert result.exceptions.overdue_count == 3
    assert result.exceptions.failed_count == 1
    assert result.exceptions.incomplete_count == 1
    assert result.exceptions.blocked_rule_count == 3
    assert [item.reason for item in result.occurrences if item.state == "exception"] == [
        "overdue_unmaterialized",
        "failed_execution",
        "incomplete_execution",
    ]
    scheduled = next(item for item in result.occurrences if item.state == "scheduled")
    assert scheduled.scheduled_for == as_of


def test_future_forecast_preserves_interval_phase_from_persisted_cursor(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast future phase")
    account, expense, _ = _create_budget_references(client, headers, "2026-09")
    cursor = datetime(2026, 8, 1, tzinfo=UTC)
    rule = _rule(
        client,
        headers,
        name="Every second day",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="1.0000",
        rrule="FREQ=DAILY;INTERVAL=2;BYHOUR=0;BYMINUTE=0",
    )
    asyncio.run(
        _set_rule_state(
            rule["id"],
            cursor=cursor,
            anchor=datetime(2026, 7, 30, tzinfo=UTC),
        )
    )

    result = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 9, 1),
            "RUB",
            datetime(2026, 7, 30, tzinfo=UTC),
        )
    )
    assert result.period_state == "open_future"
    assert result.occurrences[0].scheduled_for == datetime(2026, 9, 2, tzinfo=UTC)
    assert all(item.scheduled_for.day % 2 == 0 for item in result.occurrences)


def test_workspace_month_bounds_and_rule_timezone_are_evaluated_in_utc(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast timezone bounds")
    account, expense, _ = _create_budget_references(client, headers, "2026-08")
    cursor = datetime(2026, 7, 31, 22, tzinfo=UTC)
    rule = _rule(
        client,
        headers,
        name="Berlin month start",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="2.0000",
        rrule="FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0",
        timezone="Europe/Berlin",
    )
    asyncio.run(_set_rule_state(rule["id"], cursor=cursor, anchor=cursor))

    result = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            datetime(2026, 7, 31, 19, tzinfo=UTC),
        )
    )
    assert result.forecast.expense == Decimal("2.0000")
    assert [item.scheduled_for for item in result.occurrences] == [cursor]
    assert result.occurrences[0].scheduled_for_workspace_local == datetime(
        2026, 8, 1, 3, tzinfo=result.occurrences[0].scheduled_for_workspace_local.tzinfo
    )


def test_pending_draft_follows_current_period_currency_and_splits(client: TestClient) -> None:
    identity, headers = _register(client, "Forecast moved draft")
    rub_account, first_category, _ = _create_budget_references(client, headers, "2026-08")
    second_category = _category(client, headers, "expense")
    september = _budget(
        client,
        headers,
        "2026-09",
        "RUB",
        allocations=[
            {"category_id": first_category["id"], "planned_amount": "100.0000"},
            {"category_id": second_category["id"], "planned_amount": "100.0000"},
        ],
    )
    assert september.status_code == 200, september.text
    usd_account = _account(client, headers, currency="USD")
    usd_budget = _budget(client, headers, "2026-09", "USD")
    assert usd_budget.status_code == 200, usd_budget.text
    scheduled_for = datetime(2026, 8, 31, 12, tzinfo=UTC)
    rule_payload = _rule(
        client,
        headers,
        name="Moved split draft",
        account_id=rub_account["id"],
        category_id=first_category["id"],
        transaction_type="expense",
        amount="250.0000",
        mode="draft",
        rrule="FREQ=MONTHLY;BYMONTHDAY=31;BYHOUR=12;BYMINUTE=0",
    )
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=scheduled_for, anchor=scheduled_for))

    async def execute_and_move() -> str:
        async with AsyncSessionFactory() as session:
            rule = await session.get(RecurringRule, uuid.UUID(rule_payload["id"]))
            assert rule is not None
            execution, _ = await recurring_rules.execute_rule(
                session,
                rule,
                scheduled_for=scheduled_for,
                idempotency_key=f"moved-draft-{uuid.uuid4()}",
                service_account_id=None,
                initiated_by=rule.created_by,
                request_id=str(uuid.uuid4()),
                trigger_type="test",
            )
            assert execution.transaction_id is not None
            transaction = await session.get(FinancialTransaction, execution.transaction_id)
            assert transaction is not None
            transaction.occurred_at = datetime(2026, 9, 2, 8, tzinfo=UTC)
            transaction.amount = Decimal("100.0000")
            session.add_all(
                [
                    TransactionSplit(
                        transaction_id=transaction.id,
                        category_id=uuid.UUID(first_category["id"]),
                        amount=Decimal("40.0000"),
                    ),
                    TransactionSplit(
                        transaction_id=transaction.id,
                        category_id=uuid.UUID(second_category["id"]),
                        amount=Decimal("60.0000"),
                    ),
                ]
            )
            rule.is_active = False
            await session.commit()
            return str(transaction.id)

    transaction_id = asyncio.run(execute_and_move())
    august = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            datetime(2026, 8, 15, tzinfo=UTC),
        )
    )
    assert august.forecast.expense == ZERO

    september_rub = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 9, 1),
            "RUB",
            datetime(2026, 8, 15, tzinfo=UTC),
        )
    )
    assert september_rub.forecast.pending_draft_expense == Decimal("100.0000")
    assert september_rub.forecast.pending_draft_occurrence_count == 1
    assert [item.forecast_expense for item in september_rub.category_forecast] == [
        Decimal("40.0000"),
        Decimal("60.0000"),
    ]
    pending = next(item for item in september_rub.occurrences if item.state == "pending_draft")
    assert str(pending.transaction_id) == transaction_id
    assert pending.scheduled_for == scheduled_for
    assert pending.effective_at == datetime(2026, 9, 2, 8, tzinfo=UTC)
    assert pending.amount_source == "linked_transaction"

    asyncio.run(
        _set_rule_state(
            rule_payload["id"],
            cursor=None,
            active=False,
            deleted=True,
        )
    )
    deleted_rule_pending = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 9, 1),
            "RUB",
            datetime(2026, 8, 15, tzinfo=UTC),
        )
    )
    assert deleted_rule_pending.forecast.pending_draft_expense == Decimal("100.0000")
    assert deleted_rule_pending.forecast.pending_draft_occurrence_count == 1

    async def move_currency() -> None:
        async with AsyncSessionFactory() as session:
            transaction = await session.get(FinancialTransaction, uuid.UUID(transaction_id))
            assert transaction is not None
            transaction.currency = "USD"
            transaction.account_id = uuid.UUID(usd_account["id"])
            await session.commit()

    asyncio.run(move_currency())
    rub_after_move = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 9, 1),
            "RUB",
            datetime(2026, 8, 15, tzinfo=UTC),
        )
    )
    usd_after_move = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 9, 1),
            "USD",
            datetime(2026, 8, 15, tzinfo=UTC),
        )
    )
    assert rub_after_move.forecast.pending_draft_expense == ZERO
    assert usd_after_move.forecast.pending_draft_expense == Decimal("100.0000")


@pytest.mark.parametrize("ledger_status", ["confirmed", "reconciled"])
def test_effective_recurring_transaction_is_actual_not_forecast(
    client: TestClient,
    ledger_status: str,
) -> None:
    identity, headers = _register(client, "Forecast actual")
    account, _, income = _create_budget_references(client, headers, "2026-08")
    scheduled_for = datetime(2026, 8, 15, tzinfo=UTC)
    rule_payload = _rule(
        client,
        headers,
        name="Materialized income",
        account_id=account["id"],
        category_id=income["id"],
        transaction_type="income",
        amount="99.9999",
        mode="confirmed",
    )
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=scheduled_for, anchor=scheduled_for))

    async def execute() -> None:
        async with AsyncSessionFactory() as session:
            rule = await session.get(RecurringRule, uuid.UUID(rule_payload["id"]))
            assert rule is not None
            execution, _ = await recurring_rules.execute_rule(
                session,
                rule,
                scheduled_for=scheduled_for,
                idempotency_key=f"actual-{uuid.uuid4()}",
                service_account_id=None,
                initiated_by=rule.created_by,
                request_id=str(uuid.uuid4()),
                trigger_type="test",
            )
            assert execution.transaction_id is not None
            transaction = await session.get(FinancialTransaction, execution.transaction_id)
            assert transaction is not None
            transaction.status = ledger_status
            await session.commit()

    asyncio.run(execute())
    result = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            datetime(2026, 8, 10, tzinfo=UTC),
        )
    )
    assert result.actual.income == Decimal("99.9999")
    assert result.forecast.income == ZERO
    assert result.projected.income == Decimal("99.9999")
    assert result.materialized_actual_occurrence_count == 1


def test_deleted_linked_transaction_is_exception_not_primary_forecast(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast deleted linked transaction")
    account, expense, _ = _create_budget_references(client, headers, "2026-08")
    scheduled_for = datetime(2026, 8, 15, tzinfo=UTC)
    rule_payload = _rule(
        client,
        headers,
        name="Deleted materialization",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="11.0000",
    )
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=scheduled_for, anchor=scheduled_for))

    async def execute_and_delete() -> None:
        async with AsyncSessionFactory() as session:
            rule = await session.get(RecurringRule, uuid.UUID(rule_payload["id"]))
            assert rule is not None
            execution, _ = await recurring_rules.execute_rule(
                session,
                rule,
                scheduled_for=scheduled_for,
                idempotency_key=f"deleted-linked-{uuid.uuid4()}",
                service_account_id=None,
                initiated_by=rule.created_by,
                request_id=str(uuid.uuid4()),
                trigger_type="test",
            )
            assert execution.transaction_id is not None
            transaction = await session.get(FinancialTransaction, execution.transaction_id)
            assert transaction is not None
            transaction.deleted_at = datetime(2026, 8, 16, tzinfo=UTC)
            await session.commit()

    asyncio.run(execute_and_delete())
    result = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            datetime(2026, 8, 10, tzinfo=UTC),
        )
    )
    assert result.actual.expense == ZERO
    assert result.forecast.expense == ZERO
    assert result.exceptions.materialized_excluded_count == 1
    assert result.occurrences[0].reason == "linked_transaction_excluded"


def test_forecast_reuses_budget_actual_refund_split_adjustment_and_rollover_semantics(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast actual semantics")
    account, first_category, _ = _create_budget_references(client, headers, "2026-08")
    second_category = _category(client, headers, "expense")
    current = client.get("/api/v1/budgets/2026-08/RUB", headers=headers).json()
    updated = _budget(
        client,
        headers,
        "2026-08",
        "RUB",
        planned_income="1000.0000",
        rollover_policy="full",
        version=current["version"],
        allocations=[
            {"category_id": first_category["id"], "planned_amount": "100.0000"},
            {"category_id": second_category["id"], "planned_amount": "100.0000"},
        ],
    )
    assert updated.status_code == 200, updated.text
    expense = _transaction(
        client,
        headers,
        account_id=account["id"],
        transaction_type="expense",
        amount="100.0000",
        occurred_at="2026-08-05T10:00:00Z",
        splits=[
            {"category_id": first_category["id"], "amount": "40.0000"},
            {"category_id": second_category["id"], "amount": "60.0000"},
        ],
    )
    _transaction(
        client,
        headers,
        account_id=account["id"],
        transaction_type="refund",
        amount="10.0000",
        occurred_at="2026-08-06T10:00:00Z",
        related_transaction_id=expense["id"],
    )
    _transaction(
        client,
        headers,
        account_id=account["id"],
        transaction_type="adjustment",
        amount="5.0000",
        occurred_at="2026-08-07T10:00:00Z",
    )

    async def reconcile_original() -> None:
        async with AsyncSessionFactory() as session:
            transaction = await session.get(FinancialTransaction, uuid.UUID(expense["id"]))
            assert transaction is not None
            transaction.status = "reconciled"
            await session.commit()

    asyncio.run(reconcile_original())
    group_before = client.get("/api/v1/budgets/2026-08/RUB", headers=headers).json()
    result = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            datetime(2026, 8, 10, tzinfo=UTC),
            include_occurrences=False,
        )
    )
    group_after = client.get("/api/v1/budgets/2026-08/RUB", headers=headers).json()

    assert result.actual.income == ZERO
    assert result.actual.expense == Decimal("90.0000")
    assert result.actual.adjustment == Decimal("5.0000")
    assert result.actual.net_cashflow == Decimal("-85.0000")
    assert result.forecast.income == ZERO
    assert result.forecast.expense == ZERO
    assert result.projected.net_cashflow == Decimal("-85.0000")
    assert [item.actual_expense for item in result.category_forecast] == [
        Decimal("36.0000"),
        Decimal("54.0000"),
    ]
    assert group_before["rollover"] == group_after["rollover"]
    assert group_before["planning_capacity"] == group_after["planning_capacity"]
    assert group_before["unallocated"] == group_after["unallocated"]


def test_past_open_and_closed_months_have_zero_forecast(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, headers = _register(client, "Forecast historical")
    account, expense, _ = _create_budget_references(client, headers, "2026-07")
    rule_payload = _rule(
        client,
        headers,
        name="Current mutable rule",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="500.0000",
    )
    asyncio.run(
        _set_rule_state(
            rule_payload["id"],
            cursor=datetime(2026, 7, 15, tzinfo=UTC),
            anchor=datetime(2026, 7, 15, tzinfo=UTC),
        )
    )
    past = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 7, 1),
            "RUB",
            datetime(2026, 8, 10, tzinfo=UTC),
        )
    )
    assert past.period_state == "open_past"
    assert past.forecast.expense == ZERO
    assert past.forecast_basis == "none"

    prepared = _prepare(client, headers, "2026-07")
    _confirm(client, headers, "2026-07", prepared)

    async def immutable_close_state() -> tuple[object, ...]:
        async with AsyncSessionFactory() as session:
            closure = await session.scalar(
                select(MonthClosure).where(
                    MonthClosure.workspace_id == uuid.UUID(identity["workspace"]["id"]),
                    MonthClosure.period_month == date(2026, 7, 1),
                )
            )
            assert closure is not None
            assert closure.current_revision_id is not None
            revision = await session.get(MonthCloseRevision, closure.current_revision_id)
            assert revision is not None
            return (
                closure.prepare_token,
                closure.prepared_fingerprint,
                closure.current_revision_id,
                dict(revision.snapshot),
            )

    close_before = asyncio.run(immutable_close_state())

    async def fail_if_called(*args: object, **kwargs: object) -> list[RecurringRule]:
        raise AssertionError("closed forecast must not load recurring rules")

    monkeypatch.setattr(budget_forecasts.repository, "active_rules", fail_if_called)
    closed = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 7, 1),
            "RUB",
            datetime(2026, 8, 10, tzinfo=UTC),
        )
    )
    assert closed.period_state == "closed"
    assert closed.projection_source == "month_close_snapshot"
    assert closed.forecast_basis == "none"
    assert closed.forecast.expense == ZERO
    assert closed.occurrences == []
    assert asyncio.run(immutable_close_state()) == close_before


def test_skipped_executor_advances_cursor_and_does_not_block_later_forecast(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast skipped")
    account, expense, _ = _create_budget_references(client, headers, "2026-08")
    scheduled_for = datetime(2026, 8, 12, tzinfo=UTC)
    rule_payload = _rule(
        client,
        headers,
        name="Skipped terminal",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="5.0000",
        rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
    )
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=scheduled_for, anchor=scheduled_for))

    async def skip_then_resume() -> tuple[str, datetime, datetime]:
        async with AsyncSessionFactory() as session:
            rule = await session.get(RecurringRule, uuid.UUID(rule_payload["id"]))
            assert rule is not None
            rule.is_active = False
            execution, _ = await recurring_rules.execute_rule(
                session,
                rule,
                scheduled_for=scheduled_for,
                idempotency_key=f"skipped-{uuid.uuid4()}",
                service_account_id=None,
                initiated_by=rule.created_by,
                request_id=str(uuid.uuid4()),
                trigger_type="test",
            )
            assert rule.next_run_at is not None
            advanced = rule.next_run_at
            rule.is_active = True
            await session.commit()
            assert rule.last_run_at is not None
            return execution.status, advanced, rule.last_run_at

    status, advanced, last_run = asyncio.run(skip_then_resume())
    assert status == "skipped"
    assert advanced > scheduled_for
    assert last_run == scheduled_for
    result = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            datetime(2026, 8, 12, tzinfo=UTC),
        )
    )
    assert result.exceptions.blocked_rule_count == 0
    assert result.forecast.expense > ZERO


def test_reminder_executor_advances_cursor_and_remains_advisory_only(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast reminder execution")
    account, expense, _ = _create_budget_references(client, headers, "2026-08")
    scheduled_for = datetime(2026, 8, 12, tzinfo=UTC)
    rule_payload = _rule(
        client,
        headers,
        name="Executed reminder",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="8.0000",
        mode="reminder_only",
        rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
    )
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=scheduled_for, anchor=scheduled_for))

    async def execute_reminder() -> tuple[str, datetime]:
        async with AsyncSessionFactory() as session:
            rule = await session.get(RecurringRule, uuid.UUID(rule_payload["id"]))
            assert rule is not None
            execution, _ = await recurring_rules.execute_rule(
                session,
                rule,
                scheduled_for=scheduled_for,
                idempotency_key=f"reminder-{uuid.uuid4()}",
                service_account_id=None,
                initiated_by=rule.created_by,
                request_id=str(uuid.uuid4()),
                trigger_type="test",
            )
            assert rule.next_run_at is not None
            return execution.status, rule.next_run_at

    status, advanced = asyncio.run(execute_reminder())
    assert status == "reminder_sent"
    assert advanced > scheduled_for
    result = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            scheduled_for,
        )
    )
    assert result.forecast.income == ZERO
    assert result.forecast.expense == ZERO
    assert result.advisory.expense > ZERO
    assert result.exceptions.blocked_rule_count == 0


def test_failed_executor_keeps_cursor_and_blocks_synthetic_future_schedule(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast failed execution")
    account, expense, _ = _create_budget_references(client, headers, "2026-08")
    scheduled_for = datetime(2026, 8, 9, tzinfo=UTC)
    rule_payload = _rule(
        client,
        headers,
        name="Failed canonical cursor",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="9.0000",
        rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
    )
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=scheduled_for, anchor=scheduled_for))

    async def fail_execution() -> tuple[str, datetime]:
        async with AsyncSessionFactory() as session:
            category = await session.get(Category, uuid.UUID(expense["id"]))
            rule = await session.get(RecurringRule, uuid.UUID(rule_payload["id"]))
            assert category is not None
            assert rule is not None
            category.is_archived = True
            await session.commit()
            with pytest.raises(ApiError):
                await recurring_rules.execute_rule(
                    session,
                    rule,
                    scheduled_for=scheduled_for,
                    idempotency_key=f"failed-{uuid.uuid4()}",
                    service_account_id=None,
                    initiated_by=rule.created_by,
                    request_id=str(uuid.uuid4()),
                    trigger_type="test",
                )
            execution = await session.scalar(
                select(RecurringRuleExecution).where(
                    RecurringRuleExecution.rule_id == rule.id,
                    RecurringRuleExecution.scheduled_for == scheduled_for,
                )
            )
            await session.refresh(rule)
            assert execution is not None
            assert rule.next_run_at is not None
            return execution.status, rule.next_run_at

    status, cursor = asyncio.run(fail_execution())
    assert status == "failed"
    assert cursor == scheduled_for
    result = asyncio.run(
        _forecast(
            identity["workspace"]["id"],
            date(2026, 8, 1),
            "RUB",
            datetime(2026, 8, 10, tzinfo=UTC),
        )
    )
    assert result.forecast.expense == ZERO
    assert result.exceptions.failed_count == 1
    assert result.exceptions.blocked_rule_count == 1


def test_occurrence_cap_is_hard_and_forecast_has_no_write_side_effects(client: TestClient) -> None:
    identity, headers = _register(client, "Forecast cap")
    account, expense, _ = _create_budget_references(client, headers, "2026-08")
    rule_payload = _rule(
        client,
        headers,
        name="Pathological",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="1.0000",
        rrule=(
            "FREQ=DAILY;BYHOUR="
            + ",".join(str(value) for value in range(24))
            + ";BYMINUTE="
            + ",".join(str(value) for value in range(60))
        ),
    )
    cursor = datetime(2026, 8, 1, tzinfo=UTC)
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=cursor, anchor=cursor))

    async def counts() -> tuple[int, ...]:
        async with AsyncSessionFactory() as session:
            models = (
                FinancialTransaction,
                RecurringRuleExecution,
                RecurringRule,
                BudgetPeriod,
                BudgetAllocation,
                BudgetPlanRevision,
                AuditLog,
                SyncOutbox,
                AutomationRun,
            )
            values: list[int] = []
            for model in models:
                values.append(
                    int(await session.scalar(select(func.count()).select_from(model)) or 0)
                )
            return tuple(values)

    before = asyncio.run(counts())
    with pytest.raises(ApiError) as error:
        asyncio.run(
            _forecast(
                identity["workspace"]["id"],
                date(2026, 8, 1),
                "RUB",
                cursor,
                include_occurrences=False,
            )
        )
    assert error.value.status_code == 422
    assert error.value.code == "BUDGET_FORECAST_LIMIT_EXCEEDED"
    assert error.value.details == {"limit": 2000, "rule_id": rule_payload["id"]}
    assert asyncio.run(counts()) == before


def test_forecast_route_is_distinct_readable_and_missing_budget_is_404(client: TestClient) -> None:
    _, headers = _register(client, "Forecast route")
    _create_budget_references(client, headers, "2027-01")
    route = client.get("/api/v1/budgets/2027-01/RUB/forecast", headers=headers)
    assert route.status_code == 200, route.text
    assert route.json()["period_state"] == "open_future"
    assert route.json()["forecast"]["income"] == "0.0000"

    missing = client.get("/api/v1/budgets/2027-01/USD/forecast", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "BUDGET_NOT_FOUND"

    unauthenticated = client.get("/api/v1/budgets/2027-01/RUB/forecast")
    assert unauthenticated.status_code == 401

    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/budgets/{period}/{currency}" in paths
    assert "/api/v1/budgets/{period}/{currency}/forecast" in paths


def test_forecast_route_allows_viewer_and_isolates_other_workspace(client: TestClient) -> None:
    identity, headers = _register(client, "Forecast permissions")
    _create_budget_references(client, headers, "2026-08")

    async def viewer_headers() -> dict[str, str]:
        async with AsyncSessionFactory() as session:
            email = f"forecast-viewer-{uuid.uuid4()}@test.local"
            viewer = User(
                email=email,
                normalized_email=email,
                display_name="Forecast Viewer",
                locale="ru-RU",
                timezone="Asia/Yekaterinburg",
                is_active=True,
                password_hash=None,
                email_verified=True,
            )
            session.add(viewer)
            await session.flush()
            workspace_id = uuid.UUID(identity["workspace"]["id"])
            session.add(
                WorkspaceMember(workspace_id=workspace_id, user_id=viewer.id, role="viewer")
            )
            await session.commit()
            return {"X-User-ID": str(viewer.id), "X-Workspace-ID": str(workspace_id)}

    viewer = asyncio.run(viewer_headers())
    assert client.get("/api/v1/budgets/2026-08/RUB/forecast", headers=viewer).status_code == 200

    _, other_headers = _register(client, "Forecast other workspace")
    isolated = client.get(
        "/api/v1/budgets/2026-08/RUB/forecast",
        headers=other_headers,
    )
    assert isolated.status_code == 404


def test_successful_forecast_has_no_writes_and_query_count_is_occurrence_bounded(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast query bound")
    account, expense, _ = _create_budget_references(client, headers, "2026-08")
    rule_payload = _rule(
        client,
        headers,
        name="Query-bound schedule",
        account_id=account["id"],
        category_id=expense["id"],
        transaction_type="expense",
        amount="1.0000",
        rrule="FREQ=YEARLY;BYMONTH=7;BYMONTHDAY=31;BYHOUR=19;BYMINUTE=0",
    )
    cursor = datetime(2026, 7, 31, 19, tzinfo=UTC)
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=cursor, anchor=cursor))

    async def table_counts() -> tuple[int, ...]:
        async with AsyncSessionFactory() as session:
            models = (
                FinancialTransaction,
                RecurringRuleExecution,
                RecurringRule,
                BudgetPeriod,
                BudgetAllocation,
                BudgetPlanRevision,
                AuditLog,
                SyncOutbox,
                AutomationRun,
            )
            values: list[int] = []
            for model in models:
                values.append(
                    int(await session.scalar(select(func.count()).select_from(model)) or 0)
                )
            return tuple(values)

    def measured_forecast() -> tuple[Any, int]:
        statements = 0

        def count_statement(*args: object, **kwargs: object) -> None:
            nonlocal statements
            statements += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
        try:
            result = asyncio.run(
                _forecast(
                    identity["workspace"]["id"],
                    date(2026, 8, 1),
                    "RUB",
                    cursor,
                    include_occurrences=False,
                )
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_statement)
        return result, statements

    before_sparse = asyncio.run(table_counts())
    sparse, sparse_queries = measured_forecast()
    assert sparse.forecast.scheduled_occurrence_count == 1
    assert asyncio.run(table_counts()) == before_sparse

    async def make_dense() -> None:
        async with AsyncSessionFactory() as session:
            rule = await session.get(RecurringRule, uuid.UUID(rule_payload["id"]))
            assert rule is not None
            rule.schedule_rrule = (
                "FREQ=DAILY;BYHOUR=" + ",".join(str(hour) for hour in range(24)) + ";BYMINUTE=0"
            )
            rule.next_run_at = cursor
            rule.created_at = cursor
            await session.commit()

    asyncio.run(make_dense())
    before_dense = asyncio.run(table_counts())
    dense, dense_queries = measured_forecast()
    assert dense.forecast.scheduled_occurrence_count == 24 * 31
    assert dense_queries == sparse_queries
    assert asyncio.run(table_counts()) == before_dense


def test_repeatable_read_snapshot_never_mixes_new_actual_with_old_schedule(
    client: TestClient,
) -> None:
    identity, headers = _register(client, "Forecast snapshot race")
    account, _, income = _create_budget_references(client, headers, "2026-08")
    cursor = datetime(2026, 8, 20, tzinfo=UTC)
    rule_payload = _rule(
        client,
        headers,
        name="Race income",
        account_id=account["id"],
        category_id=income["id"],
        transaction_type="income",
        amount="10.0000",
        rrule="FREQ=MONTHLY;BYMONTHDAY=20;BYHOUR=0;BYMINUTE=0",
    )
    asyncio.run(_set_rule_state(rule_payload["id"], cursor=cursor, anchor=cursor))

    async def race() -> tuple[Any, Any]:
        worker = AsyncSessionFactory()
        try:
            rule = await worker.get(RecurringRule, uuid.UUID(rule_payload["id"]))
            assert rule is not None
            run = AutomationRun(
                workspace_id=uuid.UUID(identity["workspace"]["id"]),
                automation_type="recurring_rule",
                trigger_type="test",
                idempotency_key=f"race-{uuid.uuid4()}",
                status="completed",
                started_at=cursor,
                finished_at=cursor,
                created_at=cursor,
            )
            transaction = FinancialTransaction(
                workspace_id=uuid.UUID(identity["workspace"]["id"]),
                occurred_at=cursor,
                transaction_type="income",
                amount=Decimal("10.0000"),
                currency="RUB",
                account_id=uuid.UUID(account["id"]),
                category_id=uuid.UUID(income["id"]),
                status="confirmed",
                source="automation",
                external_id=f"race:{uuid.uuid4()}",
                created_by=rule.created_by,
                updated_by=rule.created_by,
            )
            worker.add_all([run, transaction])
            await worker.flush()
            worker.add(
                RecurringRuleExecution(
                    rule_id=rule.id,
                    scheduled_for=cursor,
                    automation_run_id=run.id,
                    transaction_id=transaction.id,
                    status="confirmed_created",
                    created_at=cursor,
                    completed_at=cursor,
                )
            )
            rule.last_run_at = cursor
            rule.next_run_at = datetime(2026, 9, 20, tzinfo=UTC)
            await worker.flush()

            manager = asynccontextmanager(get_forecast_session)
            async with manager() as snapshot:
                workspace = await snapshot.get(Workspace, uuid.UUID(identity["workspace"]["id"]))
                assert workspace is not None
                # Establish the snapshot before the worker commit.
                assert (
                    await budget_forecasts.repository.confirmed_planning_snapshot(
                        snapshot, workspace.id, date(2026, 8, 1)
                    )
                    is None
                )
                await worker.commit()
                before_commit_snapshot = await budget_forecasts.get_forecast(
                    snapshot,
                    workspace,
                    date(2026, 8, 1),
                    "RUB",
                    as_of=datetime(2026, 8, 10, tzinfo=UTC),
                    include_occurrences=False,
                )
            after_commit_snapshot = await _forecast(
                identity["workspace"]["id"],
                date(2026, 8, 1),
                "RUB",
                datetime(2026, 8, 10, tzinfo=UTC),
                include_occurrences=False,
            )
            return before_commit_snapshot, after_commit_snapshot
        finally:
            await worker.close()

    old, new = asyncio.run(race())
    assert old.actual.income == ZERO
    assert old.forecast.income == Decimal("10.0000")
    assert new.actual.income == Decimal("10.0000")
    assert new.forecast.income == ZERO
