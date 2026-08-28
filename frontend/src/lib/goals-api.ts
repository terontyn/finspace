import { apiClient } from "@/lib/api-client";
import type {
  Goal,
  GoalContributionCommandResponse,
  GoalContributionCreateRequest,
  GoalContributionPage,
  GoalCorrectionCreateRequest,
  GoalCreateRequest,
  GoalListFilters,
  GoalPage,
  GoalUpdateRequest,
} from "@/types/goals";

function goalPath(goalId?: string): string {
  return goalId ? `/api/v1/goals/${encodeURIComponent(goalId)}` : "/api/v1/goals";
}

function commandHeaders(idempotencyKey: string): HeadersInit {
  return { "X-Idempotency-Key": idempotencyKey };
}

export function listGoals(filters: GoalListFilters = {}): Promise<GoalPage> {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.currency) params.set("currency", filters.currency);
  if (filters.includeDeleted) params.set("include_deleted", "true");
  if (filters.search) params.set("search", filters.search);
  params.set("limit", String(filters.limit ?? 20));
  params.set("offset", String(filters.offset ?? 0));
  return apiClient.get(`${goalPath()}?${params.toString()}`);
}

export function getGoal(goalId: string, includeDeleted = false): Promise<Goal> {
  return apiClient.get(`${goalPath(goalId)}${includeDeleted ? "?include_deleted=true" : ""}`);
}

export function createGoal(data: GoalCreateRequest, key: string): Promise<Goal> {
  return apiClient.request(goalPath(), {
    body: JSON.stringify(data),
    headers: commandHeaders(key),
    method: "POST",
  });
}

export function updateGoal(goalId: string, data: GoalUpdateRequest, key: string): Promise<Goal> {
  return apiClient.request(goalPath(goalId), {
    body: JSON.stringify(data),
    headers: commandHeaders(key),
    method: "PATCH",
  });
}

export function runGoalLifecycle(
  goalId: string,
  operation: "pause" | "resume" | "complete" | "reopen" | "cancel",
  version: number,
  key: string,
): Promise<Goal> {
  return apiClient.request(`${goalPath(goalId)}/${operation}`, {
    body: JSON.stringify({ version }),
    headers: commandHeaders(key),
    method: "POST",
  });
}

export function deleteGoal(goalId: string, version: number, key: string): Promise<Goal> {
  return apiClient.request(`${goalPath(goalId)}?version=${version}`, {
    headers: commandHeaders(key),
    method: "DELETE",
  });
}

export function restoreGoal(goalId: string, version: number, key: string): Promise<Goal> {
  return apiClient.request(`${goalPath(goalId)}/restore`, {
    body: JSON.stringify({ version }),
    headers: commandHeaders(key),
    method: "POST",
  });
}

export function listGoalContributions(
  goalId: string,
  offset: number,
  limit = 20,
  includeDeleted = false,
): Promise<GoalContributionPage> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (includeDeleted) params.set("include_deleted", "true");
  return apiClient.get(`${goalPath(goalId)}/contributions?${params.toString()}`);
}

export function addGoalContribution(
  goalId: string,
  data: GoalContributionCreateRequest,
  key: string,
): Promise<GoalContributionCommandResponse> {
  return apiClient.request(`${goalPath(goalId)}/contributions`, {
    body: JSON.stringify(data),
    headers: commandHeaders(key),
    method: "POST",
  });
}

export function correctGoalContribution(
  goalId: string,
  contributionId: string,
  data: GoalCorrectionCreateRequest,
  key: string,
): Promise<GoalContributionCommandResponse> {
  return apiClient.request(
    `${goalPath(goalId)}/contributions/${encodeURIComponent(contributionId)}/correct`,
    {
      body: JSON.stringify(data),
      headers: commandHeaders(key),
      method: "POST",
    },
  );
}
