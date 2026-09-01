import type {
  Currency,
  EntityRef,
  Money,
  PageMeta,
  Transaction,
  TransactionSource,
  TransactionStatus,
  TransactionType,
} from "./finance";

export type CategorizationTransactionType = Exclude<TransactionType, "transfer">;

export interface CategorizationRule {
  id: string;
  name: string;
  priority: number;
  is_active: boolean;
  transaction_type: CategorizationTransactionType | null;
  account_id: string | null;
  payee_id: string | null;
  counterparty_contains: string | null;
  description_contains: string | null;
  category_id: string;
  version: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface CategorizationRulePage {
  items: CategorizationRule[];
  page: PageMeta;
}

export interface CategorizationPreview {
  matched: boolean;
  rule: CategorizationRule | null;
  category: EntityRef | null;
}

export type CategorizationApplyReason = "applied" | "no_match" | "already_categorized";

export interface CategorizationApplyResult {
  applied: boolean;
  reason: CategorizationApplyReason;
  rule: CategorizationRule | null;
  category: EntityRef | null;
  transaction: Transaction;
}

/** Persisted preview item status. Machine values, never collapsed into a display enum. */
export type CategorizationPreviewStatus =
  | "matched"
  | "no_match"
  | "transfer"
  | "already_categorized"
  | "split"
  | "reconciled"
  | "closed_period"
  | "not_found";

/** Per-item apply outcome. Machine values, kept distinct for diagnostics. */
export type CategorizationApplyStatus =
  | "applied"
  | "transaction_changed"
  | "rule_changed"
  | "category_changed"
  | "already_categorized"
  | "split"
  | "transfer"
  | "reconciled"
  | "closed_period"
  | "no_match"
  | "not_found"
  | "failed";

/** Only the backend-supported filters. Nothing here is invented client-side. */
export interface CategorizationPreviewFilterSelection {
  mode: "filter";
  import_batch_id?: string;
  occurred_from?: string;
  occurred_to?: string;
  account_id?: string;
  payee_id?: string;
  transaction_type?: TransactionType;
  status?: TransactionStatus;
  source?: TransactionSource;
}

export interface CategorizationPreviewIdsSelection {
  mode: "ids";
  transaction_ids: string[];
}

export type CategorizationPreviewSelection =
  | CategorizationPreviewFilterSelection
  | CategorizationPreviewIdsSelection;

export interface CategorizationPreviewSummary {
  selected: number;
  matched: number;
  no_match: number;
  transfer: number;
  already_categorized: number;
  split: number;
  reconciled: number;
  closed_period: number;
  not_found: number;
}

export interface CategorizationPreviewHeader {
  id: string;
  workspace_id: string;
  created_by: string;
  rule_set_version: number;
  selection_mode: "ids" | "filter";
  created_at: string;
  expires_at: string;
  summary: CategorizationPreviewSummary;
}

/** Compact immutable review facts captured when the preview was created. */
export interface CategorizationPreviewTransactionSnapshot {
  transaction_id: string;
  version: number;
  occurred_at: string;
  transaction_type: TransactionType;
  amount: Money;
  currency: Currency;
  account_id: string;
  payee_id: string | null;
  counterparty: string | null;
  description: string | null;
  status: TransactionStatus;
  source: TransactionSource;
}

/**
 * One proposed row. ``rule_name`` and ``category_name`` are snapshot evidence carried by the
 * preview, so a proposal stays displayable even if the live rule or category later changes.
 */
export interface CategorizationPreviewItem {
  id: string;
  sequence: number;
  transaction_id: string;
  transaction_version: number | null;
  status: CategorizationPreviewStatus;
  transaction: CategorizationPreviewTransactionSnapshot | null;
  rule_id: string | null;
  rule_version: number | null;
  rule_name: string | null;
  category_id: string | null;
  category_version: number | null;
  category_name: string | null;
}

export interface CategorizationPreviewItemPage {
  items: CategorizationPreviewItem[];
  page: PageMeta;
}

export interface CategorizationApplyItemResult {
  item_id: string;
  transaction_id: string | null;
  status: CategorizationApplyStatus;
  error_code: string | null;
  transaction_version: number | null;
  expected_version: number | null;
  current_version: number | null;
}

export interface CategorizationApplySummary {
  requested: number;
  applied: number;
  conflicts: number;
  not_applied: number;
  failed: number;
}

export interface CategorizationApplyResponse {
  preview_id: string;
  operation_id: string;
  summary: CategorizationApplySummary;
  results: CategorizationApplyItemResult[];
}
