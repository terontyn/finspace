import { apiClient } from "@/lib/api-client";
import type {
  BudgetCopyRequest,
  BudgetGroup,
  BudgetMonth,
  BudgetRevisionPage,
  BudgetUpsertRequest,
} from "@/types/budget";

function budgetPath(period: string, currency?: string): string {
  const base = `/api/v1/budgets/${encodeURIComponent(period)}`;
  return currency ? `${base}/${encodeURIComponent(currency)}` : base;
}

function commandHeaders(idempotencyKey: string): HeadersInit {
  return { "X-Idempotency-Key": idempotencyKey };
}

export function getBudgetMonth(period: string): Promise<BudgetMonth> {
  return apiClient.get(`${budgetPath(period)}?include_deleted=true`);
}

export function putBudget(
  period: string,
  currency: string,
  data: BudgetUpsertRequest,
  idempotencyKey: string,
): Promise<BudgetGroup> {
  return apiClient.request(budgetPath(period, currency), {
    body: JSON.stringify(data),
    headers: commandHeaders(idempotencyKey),
    method: "PUT",
  });
}

export function copyBudget(
  period: string,
  currency: string,
  data: BudgetCopyRequest,
  idempotencyKey: string,
): Promise<BudgetGroup> {
  return apiClient.request(`${budgetPath(period, currency)}/copy`, {
    body: JSON.stringify(data),
    headers: commandHeaders(idempotencyKey),
    method: "POST",
  });
}

export function deleteBudget(
  period: string,
  currency: string,
  version: number,
  idempotencyKey: string,
): Promise<BudgetGroup> {
  return apiClient.request(`${budgetPath(period, currency)}?version=${version}`, {
    headers: commandHeaders(idempotencyKey),
    method: "DELETE",
  });
}

export function restoreBudget(
  period: string,
  currency: string,
  version: number,
  idempotencyKey: string,
): Promise<BudgetGroup> {
  return apiClient.request(`${budgetPath(period, currency)}/restore`, {
    body: JSON.stringify({ version }),
    headers: commandHeaders(idempotencyKey),
    method: "POST",
  });
}

export function getBudgetHistory(
  period: string,
  currency: string,
  offset: number,
  limit = 20,
): Promise<BudgetRevisionPage> {
  return apiClient.get(`${budgetPath(period, currency)}/history?limit=${limit}&offset=${offset}`);
}
