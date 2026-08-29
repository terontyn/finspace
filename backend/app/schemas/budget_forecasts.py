import uuid
from datetime import datetime
from typing import Literal

from app.schemas.common import ApiModel, CurrencyCode, Money

BudgetForecastPeriodState = Literal["open_past", "open_current", "open_future", "closed"]
BudgetForecastProjectionSource = Literal["live", "month_close_snapshot"]
BudgetForecastBasis = Literal["current_recurring_rules", "none"]
BudgetForecastMode = Literal["draft", "confirmed"]
BudgetForecastOccurrenceState = Literal[
    "scheduled",
    "pending_draft",
    "advisory",
    "informational_transfer",
    "exception",
]
BudgetForecastAmountSource = Literal["rule", "linked_transaction"]


class BudgetForecastActual(ApiModel):
    income: Money
    expense: Money
    adjustment: Money
    net_cashflow: Money


class BudgetForecastModeBreakdown(ApiModel):
    mode: BudgetForecastMode
    income: Money
    expense: Money
    occurrence_count: int


class BudgetForecastTotals(ApiModel):
    income: Money
    expense: Money
    net_cashflow: Money
    scheduled_income: Money
    scheduled_expense: Money
    pending_draft_income: Money
    pending_draft_expense: Money
    scheduled_occurrence_count: int
    pending_draft_occurrence_count: int
    occurrence_count: int
    mode_breakdown: list[BudgetForecastModeBreakdown]


class BudgetForecastProjected(ApiModel):
    income: Money
    expense: Money
    adjustment: Money
    net_cashflow: Money


class BudgetForecastAdvisory(ApiModel):
    income: Money
    expense: Money
    occurrence_count: int


class BudgetForecastTransfers(ApiModel):
    volume: Money
    occurrence_count: int


class BudgetForecastExceptions(ApiModel):
    count: int
    failed_count: int
    skipped_count: int
    materialized_excluded_count: int
    overdue_count: int
    incomplete_count: int
    blocked_rule_count: int


class BudgetCategoryForecast(ApiModel):
    category_id: uuid.UUID
    category_name: str
    allocated_amount: Money
    actual_expense: Money
    forecast_expense: Money
    projected_expense: Money
    projected_remaining: Money
    projected_usage_percent: Money | None


class BudgetForecastOccurrence(ApiModel):
    rule_id: uuid.UUID
    rule_name: str
    execution_id: uuid.UUID | None
    transaction_id: uuid.UUID | None
    scheduled_for: datetime
    effective_at: datetime
    scheduled_for_workspace_local: datetime
    rule_timezone: str
    transaction_type: str
    amount: Money
    currency: CurrencyCode
    category_id: uuid.UUID | None
    category_name: str | None
    rule_mode: str
    state: BudgetForecastOccurrenceState
    execution_status: str | None
    transaction_status: str | None
    amount_source: BudgetForecastAmountSource
    reason: str | None


class BudgetForecastResponse(ApiModel):
    budget_id: uuid.UUID
    budget_version: int
    period: str
    currency: CurrencyCode
    timezone: str
    period_state: BudgetForecastPeriodState
    projection_source: BudgetForecastProjectionSource
    forecast_basis: BudgetForecastBasis
    as_of: datetime
    generated_at: datetime
    actual: BudgetForecastActual
    forecast: BudgetForecastTotals
    projected: BudgetForecastProjected
    advisory: BudgetForecastAdvisory
    informational_transfers: BudgetForecastTransfers
    unbudgeted_forecast_expense: Money
    materialized_actual_occurrence_count: int
    exceptions: BudgetForecastExceptions
    category_forecast: list[BudgetCategoryForecast]
    occurrences: list[BudgetForecastOccurrence]
