import { apiClient } from "./api-client";

import type {
  CategorizationApplyResponse,
  CategorizationApplyStatus,
  CategorizationPreviewHeader,
  CategorizationPreviewItemPage,
  CategorizationPreviewSelection,
} from "../types/categorization";

/** Backend caps one apply at 100 explicit item ids. Surfaced in the UI before the request. */
export const MAX_APPLY_ITEMS = 100;

/** Backend maximum items page size. */
export const MAX_ITEMS_PAGE = 200;

/** Items page size used by the review table. */
export const REVIEW_PAGE_SIZE = 50;

const PREVIEWS = "/api/v1/categorization-previews";

// --- typed API client -------------------------------------------------------

export function createCategorizationPreview(
  selection: CategorizationPreviewSelection,
): Promise<CategorizationPreviewHeader> {
  return apiClient.post<CategorizationPreviewHeader>(PREVIEWS, { selection });
}

export function fetchCategorizationPreview(
  previewId: string,
): Promise<CategorizationPreviewHeader> {
  return apiClient.get<CategorizationPreviewHeader>(`${PREVIEWS}/${previewId}`);
}

export function fetchCategorizationPreviewItems(
  previewId: string,
  { limit, offset }: { limit: number; offset: number },
): Promise<CategorizationPreviewItemPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return apiClient.get<CategorizationPreviewItemPage>(`${PREVIEWS}/${previewId}/items?${query}`);
}

export function applyCategorizationPreview(
  previewId: string,
  itemIds: string[],
  idempotencyKey: string,
): Promise<CategorizationApplyResponse> {
  return apiClient.post<CategorizationApplyResponse>(
    `${PREVIEWS}/${previewId}/apply`,
    { item_ids: itemIds },
    { "X-Idempotency-Key": idempotencyKey },
  );
}

// --- idempotent apply attempts ---------------------------------------------

/**
 * Canonical form of a selected item set.
 *
 * Backend idempotency treats the ids as a set, so the comparison here is order-insensitive:
 * re-picking the same rows in a different order must keep the pending key, not mint a new one.
 */
export function canonicalItemIds(itemIds: readonly string[]): string[] {
  return [...new Set(itemIds)].sort();
}

export function sameItemSet(a: readonly string[], b: readonly string[]): boolean {
  const left = canonicalItemIds(a);
  const right = canonicalItemIds(b);
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

export type ApplyAttemptState = "idle" | "pending" | "ambiguous" | "done";

export interface ApplyAttempt {
  previewId: string;
  /** Canonical (deduplicated, sorted) selected item ids this key belongs to. */
  itemIds: string[];
  idempotencyKey: string;
  state: ApplyAttemptState;
}

function newKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // Deterministic fallback for environments without WebCrypto; still unique per attempt.
  return `apply-${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`;
}

/**
 * Reuse the pending key when the user retries the SAME logical submission, mint a new one when the
 * selection changed. An ambiguous network failure must never silently become a second operation.
 */
export function nextApplyAttempt(
  current: ApplyAttempt | null,
  previewId: string,
  itemIds: readonly string[],
): ApplyAttempt {
  const canonical = canonicalItemIds(itemIds);
  const reusable =
    current !== null &&
    current.previewId === previewId &&
    current.state !== "done" &&
    sameItemSet(current.itemIds, canonical);
  if (reusable) return { ...current, itemIds: canonical, state: "pending" };
  return { idempotencyKey: newKey(), itemIds: canonical, previewId, state: "pending" };
}

/** A pending attempt survives only while its selection is unchanged. */
export function retainAttemptForSelection(
  current: ApplyAttempt | null,
  previewId: string,
  itemIds: readonly string[],
): ApplyAttempt | null {
  if (current === null) return null;
  if (current.state === "done") return current;
  if (current.previewId !== previewId) return null;
  return sameItemSet(current.itemIds, itemIds) ? current : null;
}

// --- outcome mapping --------------------------------------------------------

export type ApplyOutcomeGroup = "applied" | "needs_preview" | "not_applicable" | "failed";

const OUTCOME_GROUPS: Record<CategorizationApplyStatus, ApplyOutcomeGroup> = {
  already_categorized: "not_applicable",
  applied: "applied",
  category_changed: "needs_preview",
  closed_period: "not_applicable",
  failed: "failed",
  no_match: "not_applicable",
  not_found: "not_applicable",
  reconciled: "not_applicable",
  rule_changed: "needs_preview",
  split: "not_applicable",
  transaction_changed: "needs_preview",
  transfer: "not_applicable",
};

export function applyOutcomeGroup(status: CategorizationApplyStatus): ApplyOutcomeGroup {
  return OUTCOME_GROUPS[status];
}

/** Human labels. Machine statuses are kept intact alongside them for diagnostics. */
export const APPLY_STATUS_LABELS: Record<CategorizationApplyStatus, string> = {
  already_categorized: "Операция уже была категоризована",
  applied: "Применено",
  category_changed: "Категория изменилась — создайте новый список",
  closed_period: "Период закрыт",
  failed: "Не удалось применить",
  no_match: "Нет подходящего правила",
  not_found: "Операция не найдена",
  reconciled: "Операция сверена и неизменяема",
  rule_changed: "Набор правил изменился — создайте новый список",
  split: "Операция разделена на части",
  transaction_changed: "Операция изменилась после составления списка",
  transfer: "Перевод не категоризуется",
};

export const PREVIEW_STATUS_LABELS = {
  already_categorized: "Уже категоризована",
  closed_period: "Период закрыт",
  matched: "Есть предложение",
  no_match: "Нет правила",
  not_found: "Не найдена",
  reconciled: "Сверена",
  split: "Разделена",
  transfer: "Перевод",
} as const;

/** A fresh preview is required once any result invalidates the proposal evidence. */
export function requiresNewPreview(statuses: readonly CategorizationApplyStatus[]): boolean {
  return statuses.some((status) => applyOutcomeGroup(status) === "needs_preview");
}
