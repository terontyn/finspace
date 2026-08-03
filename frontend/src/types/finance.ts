export type Money = string;
export type Currency = "RUB" | "EUR" | "USD";
export type AccountType =
  | "cash"
  | "debit_card"
  | "credit_card"
  | "current_account"
  | "savings"
  | "deposit"
  | "brokerage"
  | "crypto_wallet"
  | "other";
export type CategoryType = "income" | "expense" | "both";
export type TransactionType = "income" | "expense" | "transfer" | "refund" | "adjustment";
export type TransactionStatus = "draft" | "confirmed" | "reconciled" | "cancelled";

export interface PageMeta {
  limit: number;
  offset: number;
  total: number;
}

export interface Account {
  id: string;
  name: string;
  account_type: AccountType;
  currency: Currency;
  institution: string | null;
  opening_balance: Money;
  opening_balance_at: string;
  credit_limit: Money | null;
  description: string | null;
  is_archived: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface AccountBalance {
  account_id: string;
  name: string;
  currency: Currency;
  opening_balance: Money;
  balance: Money;
}

export interface Category {
  id: string;
  parent_id: string | null;
  name: string;
  category_type: CategoryType;
  color: string | null;
  icon: string | null;
  sort_order: number;
  is_archived: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface CategoryTreeItem extends Category {
  children: CategoryTreeItem[];
}

export interface EntityRef {
  id: string;
  name: string;
}

export interface Transaction {
  id: string;
  occurred_at: string;
  transaction_type: TransactionType;
  amount: Money;
  currency: Currency;
  account: EntityRef;
  target_account: EntityRef | null;
  category: EntityRef | null;
  counterparty: string | null;
  description: string | null;
  comment: string | null;
  status: TransactionStatus;
  source: "manual" | "api" | "import" | "system" | "google_sheets" | "automation" | "telegram";
  related_transaction_id: string | null;
  external_id: string | null;
  splits: Array<{ id: string; category_id: string; category_name: string; amount: Money }>;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface AuditEntry {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  before_data: Record<string, unknown> | null;
  after_data: Record<string, unknown> | null;
  created_at: string;
  request_id: string | null;
}

export interface FinancialSummaryGroup {
  currency: Currency;
  income: Money;
  expense: Money;
  net_cashflow: Money;
  transfer_volume: Money;
  transactions_count: number;
}

export interface Paged<T> {
  items: T[];
  page: PageMeta;
}

export interface ImportBatch {
  id: string;
  filename: string;
  file_type: string;
  file_size: number;
  file_sha256: string;
  status: string;
  detected_format: string | null;
  mapping: { fields?: Record<string, string>; locale?: string } | null;
  summary: Record<string, unknown> | null;
  confirmed_at: string | null;
  rolled_back_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ImportRow {
  id: string;
  source_sheet: string | null;
  source_row_number: number;
  raw_data: Record<string, unknown>;
  normalized_data: Record<string, unknown> | null;
  validation_errors: Array<{ code?: string; message?: string }> | null;
  duplicate_transaction_id: string | null;
  status: string;
  created_transaction_id: string | null;
}
