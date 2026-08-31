import type { EntityRef, PageMeta, Transaction, TransactionType } from "./finance";

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
