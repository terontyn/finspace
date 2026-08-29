import { apiClient } from "@/lib/api-client";
import type { BudgetForecastResponse } from "@/types/budget-forecast";

export interface GetBudgetForecastOptions {
  includeOccurrences?: boolean;
  signal?: AbortSignal;
}

export function budgetForecastPath(
  period: string,
  currency: string,
  includeOccurrences = false,
): string {
  const path = `/api/v1/budgets/${encodeURIComponent(period)}/${encodeURIComponent(currency)}/forecast`;
  return includeOccurrences ? `${path}?include_occurrences=true` : path;
}

export function getBudgetForecast(
  period: string,
  currency: string,
  options: GetBudgetForecastOptions = {},
): Promise<BudgetForecastResponse> {
  return apiClient.request<BudgetForecastResponse>(
    budgetForecastPath(period, currency, options.includeOccurrences),
    { method: "GET", signal: options.signal },
  );
}
