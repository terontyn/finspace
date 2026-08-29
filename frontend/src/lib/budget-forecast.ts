import { ApiClientError } from "@/lib/api-client";
import type { BudgetForecastPeriodState } from "@/types/budget-forecast";

const genericMissingCodes = new Set(["API_ERROR", "NOT_FOUND", "ROUTE_NOT_FOUND"]);

export function isBudgetForecastNotFound(error: unknown): boolean {
  return error instanceof ApiClientError && error.code === "BUDGET_NOT_FOUND";
}

export function isForecastCapabilityUnavailable(error: unknown): boolean {
  if (!(error instanceof ApiClientError)) return false;
  if (error.status === 405) return true;
  return error.status === 404 && genericMissingCodes.has(error.code);
}

export function isForecastAuthError(error: unknown): boolean {
  return error instanceof ApiClientError && (error.status === 401 || error.status === 403);
}

export function isForecastAbort(error: unknown): boolean {
  return typeof error === "object" && error !== null && "name" in error && error.name === "AbortError";
}

export function forecastErrorMessage(error: unknown): string {
  if (error instanceof ApiClientError && error.code === "BUDGET_FORECAST_LIMIT_EXCEEDED") {
    return "Слишком много регулярных операций для расчёта прогноза.";
  }
  if (error instanceof ApiClientError && error.status === 0) {
    return "Не удалось обновить прогноз из-за сетевой ошибки.";
  }
  if (error instanceof ApiClientError && error.status >= 500) {
    return "Backend временно не смог рассчитать прогноз.";
  }
  return "Не удалось загрузить прогноз. Бюджет остаётся доступен.";
}

export function forecastPeriodMessage(state: BudgetForecastPeriodState): string | null {
  if (state === "open_future") return "Прогноз для выбранного будущего месяца.";
  if (state === "open_past") return "Период завершён — оставшийся прогноз равен нулю.";
  if (state === "closed") return "Исторический план зафиксирован при закрытии месяца.";
  return null;
}

export function isZeroForecastMoney(value: string): boolean {
  return /^-?0(?:\.0+)?$/.test(value);
}

export function formatForecastTimestamp(value: string, timezone: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: timezone,
    }).format(date);
  } catch {
    return value;
  }
}

export function areForecastInstantsEqual(left: string, right: string): boolean {
  const leftTime = new Date(left).getTime();
  const rightTime = new Date(right).getTime();
  return Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime === rightTime;
}

export function visualForecastPercentage(value: string | null): number | null {
  if (value === null) return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  return Math.max(0, Math.min(100, numeric));
}
