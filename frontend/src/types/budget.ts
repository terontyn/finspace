import type { PageMeta } from "@/types/finance";

export type BudgetMoney = string;
export type BudgetProjectionSource = "live" | "month_close_revision";
export type BudgetRolloverPolicy = "none" | "positive_only" | "full";
export type BudgetRevisionAction = "create" | "update" | "delete" | "restore" | "copy";

export interface BudgetRollover {
  amount: BudgetMoney;
  source_policy: BudgetRolloverPolicy;
  provisional: boolean;
}

export interface BudgetAllocation {
  id: string;
  category_id: string;
  category_name: string;
  parent_id: string | null;
  category_type: "income" | "expense" | "both";
  category_archived: boolean;
  category_deleted: boolean;
  planned: BudgetMoney;
  actual: BudgetMoney;
  remaining: BudgetMoney;
  usage_percent: BudgetMoney | null;
  note: string | null;
}

export interface BudgetGroup {
  id: string;
  workspace_id: string;
  period: string;
  currency: string;
  frozen: boolean;
  projection_source: BudgetProjectionSource;
  version: number;
  deleted_at: string | null;
  planned_income: BudgetMoney;
  rollover_policy: BudgetRolloverPolicy;
  allocated: BudgetMoney;
  actual_income: BudgetMoney;
  actual_expense: BudgetMoney;
  adjustment: BudgetMoney;
  actual_net_cashflow: BudgetMoney;
  budgeted_actual_expense: BudgetMoney;
  unbudgeted_actual_expense: BudgetMoney;
  remaining: BudgetMoney;
  rollover: BudgetRollover;
  planning_capacity: BudgetMoney;
  unallocated: BudgetMoney;
  allocations: BudgetAllocation[];
}

export interface BudgetMonth {
  period: string;
  timezone: string;
  projection_source: BudgetProjectionSource;
  historical_snapshot_available: boolean;
  groups: BudgetGroup[];
}

export interface BudgetAllocationInput {
  category_id: string;
  planned_amount: BudgetMoney;
  note: string | null;
}

export interface BudgetUpsertRequest {
  version?: number | null;
  planned_income: BudgetMoney;
  rollover_policy: BudgetRolloverPolicy;
  allocations: BudgetAllocationInput[];
}

export interface BudgetCopyRequest {
  source_period?: string;
  overwrite: boolean;
  version?: number;
}

export interface BudgetRevision {
  id: string;
  budget_period_id: string;
  revision_number: number;
  action: BudgetRevisionAction;
  snapshot: Record<string, unknown>;
  actor_user_id: string;
  request_id: string | null;
  created_at: string;
}

export interface BudgetRevisionPage {
  items: BudgetRevision[];
  page: PageMeta;
}

export interface AuthMeResponse {
  role: "viewer" | "editor" | "owner" | string;
}
