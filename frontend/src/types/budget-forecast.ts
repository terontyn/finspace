import type { BudgetMoney } from "@/types/budget";

export type BudgetForecastPeriodState = "open_past" | "open_current" | "open_future" | "closed";
export type BudgetForecastProjectionSource = "live" | "month_close_snapshot";
export type BudgetForecastBasis = "current_recurring_rules" | "none";
export type BudgetForecastMode = "draft" | "confirmed";
export type BudgetForecastOccurrenceState =
  | "scheduled"
  | "pending_draft"
  | "advisory"
  | "informational_transfer"
  | "exception";
export type BudgetForecastAmountSource = "rule" | "linked_transaction";

export interface BudgetForecastActual {
  income: BudgetMoney;
  expense: BudgetMoney;
  adjustment: BudgetMoney;
  net_cashflow: BudgetMoney;
}

export interface BudgetForecastModeBreakdown {
  mode: BudgetForecastMode;
  income: BudgetMoney;
  expense: BudgetMoney;
  occurrence_count: number;
}

export interface BudgetForecastTotals {
  income: BudgetMoney;
  expense: BudgetMoney;
  net_cashflow: BudgetMoney;
  scheduled_income: BudgetMoney;
  scheduled_expense: BudgetMoney;
  pending_draft_income: BudgetMoney;
  pending_draft_expense: BudgetMoney;
  scheduled_occurrence_count: number;
  pending_draft_occurrence_count: number;
  occurrence_count: number;
  mode_breakdown: BudgetForecastModeBreakdown[];
}

export interface BudgetForecastProjected {
  income: BudgetMoney;
  expense: BudgetMoney;
  adjustment: BudgetMoney;
  net_cashflow: BudgetMoney;
}

export interface BudgetForecastAdvisory {
  income: BudgetMoney;
  expense: BudgetMoney;
  occurrence_count: number;
}

export interface BudgetForecastTransfers {
  volume: BudgetMoney;
  occurrence_count: number;
}

export interface BudgetForecastExceptions {
  count: number;
  failed_count: number;
  skipped_count: number;
  materialized_excluded_count: number;
  overdue_count: number;
  incomplete_count: number;
  blocked_rule_count: number;
}

export interface BudgetCategoryForecast {
  category_id: string;
  category_name: string;
  allocated_amount: BudgetMoney;
  actual_expense: BudgetMoney;
  forecast_expense: BudgetMoney;
  projected_expense: BudgetMoney;
  projected_remaining: BudgetMoney;
  projected_usage_percent: BudgetMoney | null;
}

export interface BudgetForecastOccurrence {
  rule_id: string;
  rule_name: string;
  execution_id: string | null;
  transaction_id: string | null;
  scheduled_for: string;
  effective_at: string;
  scheduled_for_workspace_local: string;
  rule_timezone: string;
  transaction_type: string;
  amount: BudgetMoney;
  currency: string;
  category_id: string | null;
  category_name: string | null;
  rule_mode: string;
  state: BudgetForecastOccurrenceState;
  execution_status: string | null;
  transaction_status: string | null;
  amount_source: BudgetForecastAmountSource;
  reason: string | null;
}

export interface BudgetForecastResponse {
  budget_id: string;
  budget_version: number;
  period: string;
  currency: string;
  timezone: string;
  period_state: BudgetForecastPeriodState;
  projection_source: BudgetForecastProjectionSource;
  forecast_basis: BudgetForecastBasis;
  as_of: string;
  generated_at: string;
  actual: BudgetForecastActual;
  forecast: BudgetForecastTotals;
  projected: BudgetForecastProjected;
  advisory: BudgetForecastAdvisory;
  informational_transfers: BudgetForecastTransfers;
  unbudgeted_forecast_expense: BudgetMoney;
  materialized_actual_occurrence_count: number;
  exceptions: BudgetForecastExceptions;
  category_forecast: BudgetCategoryForecast[];
  occurrences: BudgetForecastOccurrence[];
}
