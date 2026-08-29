import assert from "node:assert/strict";
import test from "node:test";

import { ApiClientError, apiClient } from "./api-client";
import { budgetForecastPath, getBudgetForecast } from "./budget-forecast-api";
import {
  forecastErrorMessage,
  isBudgetForecastNotFound,
  isForecastCapabilityUnavailable,
  visualForecastPercentage,
} from "./budget-forecast";
import type { BudgetForecastResponse } from "@/types/budget-forecast";

test("forecast endpoint construction separates summary and lazy occurrence details", () => {
  assert.equal(budgetForecastPath("2026-08", "RUB"), "/api/v1/budgets/2026-08/RUB/forecast");
  assert.equal(budgetForecastPath("2026-08", "RUB", false), "/api/v1/budgets/2026-08/RUB/forecast");
  assert.equal(budgetForecastPath("2026-08", "RUB", true), "/api/v1/budgets/2026-08/RUB/forecast?include_occurrences=true");
});

test("forecast API uses the shared client, propagates AbortSignal and keeps money strings untouched", async () => {
  const originalRequest = apiClient.request;
  const controller = new AbortController();
  const exactMoney = "99999999999999999999.1234";
  let capturedPath = "";
  let capturedInit: RequestInit | undefined;
  apiClient.request = (<T,>(path: string, init: RequestInit = {}) => {
    capturedPath = path;
    capturedInit = init;
    return Promise.resolve({ actual: { income: exactMoney } } as unknown as T);
  }) as typeof apiClient.request;
  try {
    const result = await getBudgetForecast("2026-08", "RUB", { signal: controller.signal });
    assert.equal(capturedPath, "/api/v1/budgets/2026-08/RUB/forecast");
    assert.equal(capturedInit?.method, "GET");
    assert.equal(capturedInit?.signal, controller.signal);
    assert.equal((result as BudgetForecastResponse).actual.income, exactMoney);
  } finally {
    apiClient.request = originalRequest;
  }
});

test("forecast capability fallback never mistakes domain BUDGET_NOT_FOUND for a missing route", () => {
  const domainMissing = new ApiClientError("missing budget", "BUDGET_NOT_FOUND", 404);
  const generic404 = new ApiClientError("not found", "API_ERROR", 404);
  const route404 = new ApiClientError("not found", "ROUTE_NOT_FOUND", 404);
  const method405 = new ApiClientError("method", "API_ERROR", 405);
  const cap422 = new ApiClientError("cap", "BUDGET_FORECAST_LIMIT_EXCEEDED", 422);
  assert.equal(isBudgetForecastNotFound(domainMissing), true);
  assert.equal(isForecastCapabilityUnavailable(domainMissing), false);
  assert.equal(isForecastCapabilityUnavailable(generic404), true);
  assert.equal(isForecastCapabilityUnavailable(route404), true);
  assert.equal(isForecastCapabilityUnavailable(method405), true);
  assert.equal(isForecastCapabilityUnavailable(cap422), false);
  assert.match(forecastErrorMessage(cap422), /Слишком много регулярных операций/);
});

test("forecast percentage conversion is visual-only and clamps without changing exact money fields", () => {
  assert.equal(visualForecastPercentage("125.5000"), 100);
  assert.equal(visualForecastPercentage("-5.0000"), 0);
  assert.equal(visualForecastPercentage("33.3333"), 33.3333);
  assert.equal(visualForecastPercentage(null), null);
});
