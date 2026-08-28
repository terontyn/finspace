import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.automations import RecurringRule, RecurringRuleExecution
from app.db.models.budgets import BudgetAllocation
from app.db.models.categories import Category
from app.db.models.transactions import FinancialTransaction, TransactionSplit
from app.db.models.users import Workspace
from app.repositories import budget_forecasts as repository
from app.repositories import budgets as budget_repository
from app.schemas.budget_forecasts import (
    BudgetCategoryForecast,
    BudgetForecastActual,
    BudgetForecastAdvisory,
    BudgetForecastExceptions,
    BudgetForecastModeBreakdown,
    BudgetForecastOccurrence,
    BudgetForecastProjected,
    BudgetForecastResponse,
    BudgetForecastTotals,
    BudgetForecastTransfers,
)
from app.schemas.budgets import BudgetGroupResponse
from app.services import recurrence
from app.services.budget_actuals import BudgetActual, money, project_budget_actuals
from app.services.financial_period_guard import period_bounds

ZERO = Decimal("0.0000")
OCCURRENCE_LIMIT = 2000
EFFECTIVE_STATUSES = {"confirmed", "reconciled"}


@dataclass(slots=True)
class _Flow:
    income: Decimal = ZERO
    expense: Decimal = ZERO
    count: int = 0

    def add(self, transaction_type: str, amount: Decimal) -> bool:
        if transaction_type == "income":
            self.income = money(self.income + amount)
        elif transaction_type == "expense":
            self.expense = money(self.expense + amount)
        else:
            return False
        self.count += 1
        return True


@dataclass(slots=True)
class _ExceptionCounts:
    count: int = 0
    failed: int = 0
    skipped: int = 0
    materialized_excluded: int = 0
    overdue: int = 0
    incomplete: int = 0
    blocked_rules: set[uuid.UUID] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Occurrence:
    rule: RecurringRule
    scheduled_for: datetime
    effective_at: datetime
    transaction_type: str
    amount: Decimal
    currency: str
    category_id: uuid.UUID | None
    rule_mode: str
    state: str
    amount_source: str
    execution: RecurringRuleExecution | None = None
    transaction: FinancialTransaction | None = None
    reason: str | None = None


def _period_state(period: date, timezone: str, as_of: datetime) -> str:
    local_date = as_of.astimezone(ZoneInfo(timezone)).date()
    current = local_date.replace(day=1)
    if period < current:
        return "open_past"
    if period > current:
        return "open_future"
    return "open_current"


def _empty_forecast() -> BudgetForecastTotals:
    return BudgetForecastTotals(
        income=ZERO,
        expense=ZERO,
        net_cashflow=ZERO,
        scheduled_income=ZERO,
        scheduled_expense=ZERO,
        pending_draft_income=ZERO,
        pending_draft_expense=ZERO,
        scheduled_occurrence_count=0,
        pending_draft_occurrence_count=0,
        occurrence_count=0,
        mode_breakdown=[
            BudgetForecastModeBreakdown(
                mode="confirmed", income=ZERO, expense=ZERO, occurrence_count=0
            ),
            BudgetForecastModeBreakdown(
                mode="draft", income=ZERO, expense=ZERO, occurrence_count=0
            ),
        ],
    )


def _empty_exceptions() -> BudgetForecastExceptions:
    return BudgetForecastExceptions(
        count=0,
        failed_count=0,
        skipped_count=0,
        materialized_excluded_count=0,
        overdue_count=0,
        incomplete_count=0,
        blocked_rule_count=0,
    )


def _actual_response(actual: BudgetActual) -> BudgetForecastActual:
    return BudgetForecastActual(
        income=money(actual.income),
        expense=money(actual.expense),
        adjustment=money(actual.adjustment),
        net_cashflow=money(actual.net_cashflow),
    )


def _projected(
    actual: BudgetForecastActual, forecast: BudgetForecastTotals
) -> BudgetForecastProjected:
    return BudgetForecastProjected(
        income=money(actual.income + forecast.income),
        expense=money(actual.expense + forecast.expense),
        adjustment=money(actual.adjustment),
        net_cashflow=money(actual.net_cashflow + forecast.net_cashflow),
    )


def _usage_percent(actual: Decimal, planned: Decimal) -> Decimal | None:
    if planned == 0:
        return None
    return money(actual * Decimal("100") / planned)


def _closed_group(snapshot: dict[str, object], currency: str) -> BudgetGroupResponse | None:
    groups = snapshot.get("groups", [])
    if not isinstance(groups, list):
        return None
    for item in groups:
        if isinstance(item, dict) and item.get("currency") == currency:
            return BudgetGroupResponse.model_validate(item)
    return None


def _closed_response(
    group: BudgetGroupResponse,
    workspace: Workspace,
    period: date,
    as_of: datetime,
    generated_at: datetime,
) -> BudgetForecastResponse:
    actual = BudgetForecastActual(
        income=group.actual_income,
        expense=group.actual_expense,
        adjustment=group.adjustment,
        net_cashflow=group.actual_net_cashflow,
    )
    category_rows = [
        BudgetCategoryForecast(
            category_id=item.category_id,
            category_name=item.category_name,
            allocated_amount=item.planned,
            actual_expense=item.actual,
            forecast_expense=ZERO,
            projected_expense=item.actual,
            projected_remaining=item.remaining,
            projected_usage_percent=item.usage_percent,
        )
        for item in group.allocations
    ]
    empty = _empty_forecast()
    return BudgetForecastResponse(
        budget_id=group.id,
        budget_version=group.version,
        period=period.strftime("%Y-%m"),
        currency=group.currency,
        timezone=workspace.timezone,
        period_state="closed",
        projection_source="month_close_snapshot",
        forecast_basis="none",
        as_of=as_of,
        generated_at=generated_at,
        actual=actual,
        forecast=empty,
        projected=_projected(actual, empty),
        advisory=BudgetForecastAdvisory(income=ZERO, expense=ZERO, occurrence_count=0),
        informational_transfers=BudgetForecastTransfers(volume=ZERO, occurrence_count=0),
        unbudgeted_forecast_expense=ZERO,
        materialized_actual_occurrence_count=0,
        exceptions=_empty_exceptions(),
        category_forecast=category_rows,
        occurrences=[],
    )


def _category_rows(
    allocations: list[BudgetAllocation],
    categories: dict[uuid.UUID, Category],
    actual: BudgetActual,
    forecast_by_category: dict[uuid.UUID | None, Decimal],
) -> tuple[list[BudgetCategoryForecast], Decimal]:
    rows: list[BudgetCategoryForecast] = []
    allocated_ids = {item.category_id for item in allocations}
    for allocation in allocations:
        category = categories.get(allocation.category_id)
        category_actual = money(actual.category_expense.get(allocation.category_id, ZERO))
        category_forecast = money(forecast_by_category.get(allocation.category_id, ZERO))
        projected = money(category_actual + category_forecast)
        planned = money(allocation.planned_amount)
        rows.append(
            BudgetCategoryForecast(
                category_id=allocation.category_id,
                category_name=category.name if category is not None else "Deleted category",
                allocated_amount=planned,
                actual_expense=category_actual,
                forecast_expense=category_forecast,
                projected_expense=projected,
                projected_remaining=money(planned - projected),
                projected_usage_percent=_usage_percent(projected, planned),
            )
        )
    unbudgeted = money(
        sum(
            (
                amount
                for category_id, amount in forecast_by_category.items()
                if category_id not in allocated_ids
            ),
            start=ZERO,
        )
    )
    return rows, unbudgeted


def _allocate_pending_expense(
    transaction: FinancialTransaction,
    splits: list[TransactionSplit],
    target: dict[uuid.UUID | None, Decimal],
) -> None:
    if not splits:
        target[transaction.category_id] = money(
            target[transaction.category_id] + transaction.amount
        )
        return
    remaining = money(transaction.amount)
    for index, split in enumerate(splits):
        allocated = (
            remaining
            if index == len(splits) - 1
            else money(transaction.amount * split.amount / transaction.amount)
        )
        target[split.category_id] = money(target[split.category_id] + allocated)
        remaining = money(remaining - allocated)


def _exception_occurrence(
    rule: RecurringRule,
    scheduled_for: datetime,
    reason: str,
    *,
    execution: RecurringRuleExecution | None = None,
    transaction: FinancialTransaction | None = None,
) -> _Occurrence:
    return _Occurrence(
        rule=rule,
        scheduled_for=scheduled_for,
        effective_at=transaction.occurred_at if transaction is not None else scheduled_for,
        transaction_type=(
            transaction.transaction_type if transaction is not None else rule.transaction_type
        ),
        amount=money(transaction.amount if transaction is not None else rule.amount),
        currency=transaction.currency if transaction is not None else rule.currency,
        category_id=transaction.category_id if transaction is not None else rule.category_id,
        rule_mode=rule.creation_mode,
        state="exception",
        amount_source="linked_transaction" if transaction is not None else "rule",
        execution=execution,
        transaction=transaction,
        reason=reason,
    )


def _record_exception(
    counts: _ExceptionCounts,
    occurrence_list: list[_Occurrence],
    occurrence: _Occurrence,
    *,
    failed: bool = False,
    skipped: bool = False,
    materialized_excluded: bool = False,
    overdue: bool = False,
    incomplete: bool = False,
    blocked: bool = False,
) -> None:
    counts.count += 1
    counts.failed += int(failed)
    counts.skipped += int(skipped)
    counts.materialized_excluded += int(materialized_excluded)
    counts.overdue += int(overdue)
    counts.incomplete += int(incomplete)
    if blocked:
        counts.blocked_rules.add(occurrence.rule.id)
    occurrence_list.append(occurrence)


def _classify_overdue_cursor(
    rule: RecurringRule,
    cursor: datetime,
    record: repository.ExecutionRecord | None,
    counts: _ExceptionCounts,
    occurrence_list: list[_Occurrence],
) -> None:
    if record is None:
        _record_exception(
            counts,
            occurrence_list,
            _exception_occurrence(rule, cursor, "overdue_unmaterialized"),
            overdue=True,
            blocked=True,
        )
        return
    execution = record.execution
    transaction = record.transaction
    if execution.status == "failed":
        _record_exception(
            counts,
            occurrence_list,
            _exception_occurrence(
                rule, cursor, "failed_execution", execution=execution, transaction=transaction
            ),
            failed=True,
            overdue=True,
            blocked=True,
        )
        return
    if execution.status == "created":
        _record_exception(
            counts,
            occurrence_list,
            _exception_occurrence(
                rule, cursor, "incomplete_execution", execution=execution, transaction=transaction
            ),
            overdue=True,
            incomplete=True,
            blocked=True,
        )
        return
    _record_exception(
        counts,
        occurrence_list,
        _exception_occurrence(
            rule,
            cursor,
            "terminal_execution_cursor_not_advanced",
            execution=execution,
            transaction=transaction,
        ),
        skipped=execution.status == "skipped",
        overdue=True,
        incomplete=True,
        blocked=True,
    )


def _append_scheduled(
    rule: RecurringRule,
    scheduled_for: datetime,
    scheduled: _Flow,
    advisory: _Flow,
    transfers: list[Decimal],
    forecast_by_category: dict[uuid.UUID | None, Decimal],
    mode_flows: dict[str, _Flow],
    occurrence_list: list[_Occurrence],
) -> None:
    occurrence = _Occurrence(
        rule=rule,
        scheduled_for=scheduled_for,
        effective_at=scheduled_for,
        transaction_type=rule.transaction_type,
        amount=money(rule.amount),
        currency=rule.currency,
        category_id=rule.category_id,
        rule_mode=rule.creation_mode,
        state="scheduled",
        amount_source="rule",
    )
    if rule.creation_mode == "reminder_only":
        if not advisory.add(rule.transaction_type, money(rule.amount)):
            advisory.count += 1
        occurrence_list.append(replace(occurrence, state="advisory"))
        return
    if rule.transaction_type == "transfer":
        transfers.append(money(rule.amount))
        occurrence_list.append(replace(occurrence, state="informational_transfer"))
        return
    amount = money(rule.amount)
    if scheduled.add(rule.transaction_type, amount):
        mode_flows[rule.creation_mode].add(rule.transaction_type, amount)
        if rule.transaction_type == "expense":
            forecast_by_category[rule.category_id] = money(
                forecast_by_category[rule.category_id] + amount
            )
        occurrence_list.append(occurrence)


def _classify_materialized(
    rule: RecurringRule,
    scheduled_for: datetime,
    record: repository.ExecutionRecord,
    pending_transaction_ids: set[uuid.UUID],
    period_start: datetime,
    period_end: datetime,
    currency: str,
    counts: _ExceptionCounts,
    occurrence_list: list[_Occurrence],
    materialized_actual_ids: set[uuid.UUID],
) -> None:
    execution = record.execution
    transaction = record.transaction
    if transaction is not None:
        if transaction.status in EFFECTIVE_STATUSES and transaction.deleted_at is None:
            if (
                transaction.currency == currency
                and period_start <= transaction.occurred_at < period_end
            ):
                materialized_actual_ids.add(execution.id)
                return
            _record_exception(
                counts,
                occurrence_list,
                _exception_occurrence(
                    rule,
                    scheduled_for,
                    "materialized_outside_budget_period",
                    execution=execution,
                    transaction=transaction,
                ),
                materialized_excluded=True,
            )
            return
        if transaction.status == "draft" and transaction.deleted_at is None:
            if transaction.id not in pending_transaction_ids:
                _record_exception(
                    counts,
                    occurrence_list,
                    _exception_occurrence(
                        rule,
                        scheduled_for,
                        "pending_draft_moved_outside_budget_period",
                        execution=execution,
                        transaction=transaction,
                    ),
                    materialized_excluded=True,
                )
            return
        _record_exception(
            counts,
            occurrence_list,
            _exception_occurrence(
                rule,
                scheduled_for,
                "linked_transaction_excluded",
                execution=execution,
                transaction=transaction,
            ),
            materialized_excluded=True,
        )
        return
    if execution.status == "reminder_sent":
        return
    if execution.status == "skipped":
        _record_exception(
            counts,
            occurrence_list,
            _exception_occurrence(rule, scheduled_for, "skipped", execution=execution),
            skipped=True,
        )
        return
    if execution.status == "failed":
        _record_exception(
            counts,
            occurrence_list,
            _exception_occurrence(rule, scheduled_for, "failed_execution", execution=execution),
            failed=True,
        )
        return
    _record_exception(
        counts,
        occurrence_list,
        _exception_occurrence(
            rule, scheduled_for, "missing_linked_transaction", execution=execution
        ),
        incomplete=True,
    )
    return


def _occurrence_response(
    item: _Occurrence,
    workspace_timezone: str,
    categories: dict[uuid.UUID, Category],
) -> BudgetForecastOccurrence:
    category = categories.get(item.category_id) if item.category_id is not None else None
    return BudgetForecastOccurrence(
        rule_id=item.rule.id,
        rule_name=item.rule.name,
        execution_id=item.execution.id if item.execution is not None else None,
        transaction_id=item.transaction.id if item.transaction is not None else None,
        scheduled_for=item.scheduled_for,
        effective_at=item.effective_at,
        scheduled_for_workspace_local=item.scheduled_for.astimezone(ZoneInfo(workspace_timezone)),
        rule_timezone=item.rule.timezone,
        transaction_type=item.transaction_type,
        amount=money(item.amount),
        currency=item.currency,
        category_id=item.category_id,
        category_name=category.name if category is not None else None,
        rule_mode=item.rule_mode,
        state=cast(Any, item.state),
        execution_status=item.execution.status if item.execution is not None else None,
        transaction_status=item.transaction.status if item.transaction is not None else None,
        amount_source=cast(Any, item.amount_source),
        reason=item.reason,
    )


async def get_forecast(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    currency: str,
    *,
    as_of: datetime,
    include_occurrences: bool,
) -> BudgetForecastResponse:
    as_of = as_of.astimezone(UTC).replace(microsecond=0)
    period_start, period_end = period_bounds(period, workspace.timezone)

    # This is intentionally the first domain SELECT in the forecast snapshot.
    closed_snapshot = await repository.confirmed_planning_snapshot(session, workspace.id, period)
    if closed_snapshot is not None:
        group = _closed_group(closed_snapshot, currency)
        if group is None:
            raise ApiError(status_code=404, code="BUDGET_NOT_FOUND", message="Budget was not found")
        return _closed_response(group, workspace, period, as_of, datetime.now(UTC))

    budget = await budget_repository.get_period(
        session, workspace.id, period, currency, include_deleted=False
    )
    if budget is None:
        raise ApiError(status_code=404, code="BUDGET_NOT_FOUND", message="Budget was not found")
    allocations = (await budget_repository.allocations_for_periods(session, [budget.id]))[budget.id]
    actuals = await project_budget_actuals(session, workspace.id, period, workspace.timezone)
    actual = actuals.get(currency, BudgetActual())
    state = _period_state(period, workspace.timezone, as_of)

    if state == "open_past":
        category_map = await repository.categories(
            session, workspace.id, {item.category_id for item in allocations}
        )
        category_rows, _ = _category_rows(
            allocations, category_map, actual, defaultdict(lambda: ZERO)
        )
        actual_response = _actual_response(actual)
        empty = _empty_forecast()
        return BudgetForecastResponse(
            budget_id=budget.id,
            budget_version=budget.version,
            period=period.strftime("%Y-%m"),
            currency=currency,
            timezone=workspace.timezone,
            period_state="open_past",
            projection_source="live",
            forecast_basis="none",
            as_of=as_of,
            generated_at=datetime.now(UTC),
            actual=actual_response,
            forecast=empty,
            projected=_projected(actual_response, empty),
            advisory=BudgetForecastAdvisory(income=ZERO, expense=ZERO, occurrence_count=0),
            informational_transfers=BudgetForecastTransfers(volume=ZERO, occurrence_count=0),
            unbudgeted_forecast_expense=ZERO,
            materialized_actual_occurrence_count=0,
            exceptions=_empty_exceptions(),
            category_forecast=category_rows,
            occurrences=[],
        )

    pending_records = await repository.pending_linked_drafts(
        session,
        workspace.id,
        currency,
        start=period_start,
        end=period_end,
    )
    unique_pending: dict[uuid.UUID, repository.PendingDraftRecord] = {}
    for record in pending_records:
        unique_pending.setdefault(record.transaction.id, record)
    pending_splits = await repository.splits_for_transactions(session, set(unique_pending))

    rules = await repository.active_rules(session, workspace.id, currency)
    overdue_keys = [
        (rule.id, rule.next_run_at.astimezone(UTC).replace(microsecond=0))
        for rule in rules
        if rule.next_run_at is not None and rule.next_run_at.astimezone(UTC) < as_of
    ]
    execution_records = await repository.execution_records(
        session,
        [rule.id for rule in rules],
        start=period_start,
        end=period_end,
        cursor_keys=overdue_keys,
    )
    execution_map = {
        (
            record.execution.rule_id,
            record.execution.scheduled_for.astimezone(UTC).replace(microsecond=0),
        ): record
        for record in execution_records
    }
    rules_by_id = {rule.id: rule for rule in rules}

    scheduled = _Flow()
    pending = _Flow()
    advisory = _Flow()
    mode_flows = {"confirmed": _Flow(), "draft": _Flow()}
    transfer_amounts: list[Decimal] = []
    forecast_by_category: dict[uuid.UUID | None, Decimal] = defaultdict(lambda: ZERO)
    exception_counts = _ExceptionCounts()
    occurrence_values: list[_Occurrence] = []
    pending_transaction_ids = set(unique_pending)

    for record in unique_pending.values():
        transaction = record.transaction
        rule = record.rule
        if transaction.transaction_type in {"income", "expense"}:
            amount = money(transaction.amount)
            pending.add(transaction.transaction_type, amount)
            mode_flows["draft"].add(transaction.transaction_type, amount)
            if transaction.transaction_type == "expense":
                _allocate_pending_expense(
                    transaction,
                    pending_splits.get(transaction.id, []),
                    forecast_by_category,
                )
            occurrence_values.append(
                _Occurrence(
                    rule=rule,
                    scheduled_for=record.execution.scheduled_for,
                    effective_at=transaction.occurred_at,
                    transaction_type=transaction.transaction_type,
                    amount=amount,
                    currency=transaction.currency,
                    category_id=(
                        None if pending_splits.get(transaction.id) else transaction.category_id
                    ),
                    rule_mode="draft",
                    state="pending_draft",
                    amount_source="linked_transaction",
                    execution=record.execution,
                    transaction=transaction,
                )
            )
        elif transaction.transaction_type == "transfer":
            transfer_amounts.append(money(transaction.amount))
            occurrence_values.append(
                _Occurrence(
                    rule=rule,
                    scheduled_for=record.execution.scheduled_for,
                    effective_at=transaction.occurred_at,
                    transaction_type="transfer",
                    amount=money(transaction.amount),
                    currency=transaction.currency,
                    category_id=None,
                    rule_mode="draft",
                    state="informational_transfer",
                    amount_source="linked_transaction",
                    execution=record.execution,
                    transaction=transaction,
                )
            )
        else:
            _record_exception(
                exception_counts,
                occurrence_values,
                _exception_occurrence(
                    rule,
                    record.execution.scheduled_for,
                    "unsupported_pending_transaction_type",
                    execution=record.execution,
                    transaction=transaction,
                ),
                materialized_excluded=True,
            )

    generated_count = 0
    materialized_actual_ids: set[uuid.UUID] = set()
    processed_execution_ids: set[uuid.UUID] = set()
    expansion_start = max(period_start, as_of)
    for rule in rules:
        assert rule.next_run_at is not None
        cursor = rule.next_run_at.astimezone(UTC).replace(microsecond=0)
        if cursor < as_of:
            overdue_record = execution_map.get((rule.id, cursor))
            _classify_overdue_cursor(
                rule,
                cursor,
                overdue_record,
                exception_counts,
                occurrence_values,
            )
            if overdue_record is not None:
                processed_execution_ids.add(overdue_record.execution.id)
            continue
        if cursor >= period_end:
            continue
        try:
            expanded = recurrence.occurrences_between(
                rule.schedule_rrule,
                rule.timezone,
                first_occurrence=cursor,
                start=expansion_start,
                end=period_end,
                anchor=rule.created_at,
                limit=OCCURRENCE_LIMIT - generated_count,
            )
        except recurrence.OccurrenceExpansionLimitExceeded as exc:
            raise ApiError(
                status_code=422,
                code="BUDGET_FORECAST_LIMIT_EXCEEDED",
                message="Budget forecast occurrence limit was exceeded",
                details={"limit": OCCURRENCE_LIMIT, "rule_id": str(rule.id)},
            ) from exc
        generated_count += expanded.generated_count
        for scheduled_for in expanded.occurrences:
            execution_record = execution_map.get((rule.id, scheduled_for))
            if execution_record is None:
                _append_scheduled(
                    rule,
                    scheduled_for,
                    scheduled,
                    advisory,
                    transfer_amounts,
                    forecast_by_category,
                    mode_flows,
                    occurrence_values,
                )
            else:
                processed_execution_ids.add(execution_record.execution.id)
                _classify_materialized(
                    rule,
                    scheduled_for,
                    execution_record,
                    pending_transaction_ids,
                    period_start,
                    period_end,
                    currency,
                    exception_counts,
                    occurrence_values,
                    materialized_actual_ids,
                )

    for execution_record in execution_records:
        execution = execution_record.execution
        if execution.id in processed_execution_ids:
            continue
        if not (period_start <= execution.scheduled_for < period_end):
            continue
        replay_rule = rules_by_id.get(execution.rule_id)
        if replay_rule is None:
            continue
        _classify_materialized(
            replay_rule,
            execution.scheduled_for,
            execution_record,
            pending_transaction_ids,
            period_start,
            period_end,
            currency,
            exception_counts,
            occurrence_values,
            materialized_actual_ids,
        )

    category_ids = {item.category_id for item in allocations}
    category_ids.update(category_id for category_id in forecast_by_category if category_id)
    category_ids.update(item.category_id for item in occurrence_values if item.category_id)
    category_map = await repository.categories(session, workspace.id, category_ids)
    category_rows, unbudgeted = _category_rows(
        allocations, category_map, actual, forecast_by_category
    )

    forecast_income = money(scheduled.income + pending.income)
    forecast_expense = money(scheduled.expense + pending.expense)
    forecast = BudgetForecastTotals(
        income=forecast_income,
        expense=forecast_expense,
        net_cashflow=money(forecast_income - forecast_expense),
        scheduled_income=money(scheduled.income),
        scheduled_expense=money(scheduled.expense),
        pending_draft_income=money(pending.income),
        pending_draft_expense=money(pending.expense),
        scheduled_occurrence_count=scheduled.count,
        pending_draft_occurrence_count=pending.count,
        occurrence_count=scheduled.count + pending.count,
        mode_breakdown=[
            BudgetForecastModeBreakdown(
                mode=cast(Literal["confirmed", "draft"], mode),
                income=money(flow.income),
                expense=money(flow.expense),
                occurrence_count=flow.count,
            )
            for mode, flow in (
                ("confirmed", mode_flows["confirmed"]),
                ("draft", mode_flows["draft"]),
            )
        ],
    )
    actual_response = _actual_response(actual)
    exception_response = BudgetForecastExceptions(
        count=exception_counts.count,
        failed_count=exception_counts.failed,
        skipped_count=exception_counts.skipped,
        materialized_excluded_count=exception_counts.materialized_excluded,
        overdue_count=exception_counts.overdue,
        incomplete_count=exception_counts.incomplete,
        blocked_rule_count=len(exception_counts.blocked_rules),
    )
    occurrence_responses = (
        [
            _occurrence_response(item, workspace.timezone, category_map)
            for item in sorted(
                occurrence_values,
                key=lambda value: (
                    value.effective_at,
                    value.rule.id,
                    value.execution.id if value.execution is not None else uuid.UUID(int=0),
                ),
            )
        ]
        if include_occurrences
        else []
    )
    return BudgetForecastResponse(
        budget_id=budget.id,
        budget_version=budget.version,
        period=period.strftime("%Y-%m"),
        currency=currency,
        timezone=workspace.timezone,
        period_state=cast(Any, state),
        projection_source="live",
        forecast_basis="current_recurring_rules",
        as_of=as_of,
        generated_at=datetime.now(UTC),
        actual=actual_response,
        forecast=forecast,
        projected=_projected(actual_response, forecast),
        advisory=BudgetForecastAdvisory(
            income=money(advisory.income),
            expense=money(advisory.expense),
            occurrence_count=advisory.count,
        ),
        informational_transfers=BudgetForecastTransfers(
            volume=money(sum(transfer_amounts, start=ZERO)),
            occurrence_count=len(transfer_amounts),
        ),
        unbudgeted_forecast_expense=unbudgeted,
        materialized_actual_occurrence_count=len(materialized_actual_ids),
        exceptions=exception_response,
        category_forecast=category_rows,
        occurrences=occurrence_responses,
    )
