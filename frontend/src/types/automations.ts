import type { Money, PageMeta } from "@/types/finance";

export interface ServiceKeyMetadata {
  id: string;
  key_prefix: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ServiceAccount {
  id: string;
  name: string;
  service_type: "n8n" | "backup_agent" | "integration";
  status: "active" | "revoked" | "expired";
  permissions: string[];
  last_used_at: string | null;
  keys: ServiceKeyMetadata[];
}

export interface AutomationRun {
  id: string;
  automation_type: string;
  trigger_type: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  error_code: string | null;
  error_message: string | null;
}

export interface AutomationStatus {
  status: "healthy" | "stale" | "not_configured";
  last_heartbeat_at: string | null;
  active_service_account: ServiceAccount | null;
  recent_successful_run: AutomationRun | null;
  recent_failed_run: AutomationRun | null;
  stale_after_minutes: number;
}

export interface RecurringRule {
  id: string;
  name: string;
  rule_type: "income" | "expense" | "transfer";
  schedule_rrule: string;
  timezone: string;
  transaction_type: "income" | "expense" | "transfer";
  amount: Money;
  currency: string;
  account_id: string;
  target_account_id: string | null;
  category_id: string | null;
  creation_mode: "draft" | "confirmed" | "reminder_only";
  is_active: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  version: number;
  deleted_at: string | null;
}

export interface RecurringExecution {
  id: string;
  scheduled_for: string;
  transaction_id: string | null;
  status: string;
  completed_at: string | null;
  duplicate: boolean;
}

export interface TelegramStatus {
  linked: boolean;
  workspace_id: string | null;
  telegram_user_id: number | null;
  telegram_chat_id: number | null;
  telegram_username: string | null;
  status: string | null;
  linked_at: string | null;
  last_seen_at: string | null;
}

export interface NotificationSetting {
  id: string;
  event_type: string;
  enabled: boolean;
  schedule_time: string | null;
  timezone: string;
}

export interface MonthCloseCapabilities {
  can_prepare: boolean;
  can_confirm: boolean;
  can_reopen: boolean;
  can_view_history: boolean;
}

export interface MonthCloseIssue {
  code: string;
  severity: "blocker" | "warning" | "info";
  scope: string;
  count: number;
  message: string;
  details: Record<string, unknown>;
}

export interface MonthClosure {
  id: string;
  period_month: string;
  status: "draft" | "ready" | "blocked" | "confirmed" | "reopened";
  summary: Record<string, unknown>;
  blocking_issues: MonthCloseIssue[] | null;
  warning_issues: MonthCloseIssue[] | null;
  info_issues: MonthCloseIssue[];
  prepare_token: string | null;
  prepared_fingerprint: string | null;
  current_revision_id: string | null;
  last_reopened_at: string | null;
  last_reopened_by: string | null;
  last_reopen_reason: string | null;
  current_revision: number | null;
  capabilities: MonthCloseCapabilities;
  version: number;
  prepared_at: string | null;
  confirmed_at: string | null;
}

export interface MonthClosePeriodSummary {
  period_month: string;
  status: "not_prepared" | MonthClosure["status"];
  version: number | null;
  current_revision: number | null;
  prepared: boolean;
  blocker_count: number;
  warning_count: number;
  confirmed_at: string | null;
  reopened_at: string | null;
  capabilities: MonthCloseCapabilities;
}

export interface MonthClosurePage extends ApiPage<MonthClosure> {
  periods: MonthClosePeriodSummary[];
  closed_through: string | null;
  backup_policy: "warn" | "require_healthy";
}

export interface MonthCloseActor {
  id: string;
  display_name: string;
  display_name_source: "current_profile";
}

export interface MonthCloseRevision {
  id: string;
  revision_number: number;
  period_month: string;
  period_start_at: string;
  period_end_at: string;
  confirmed_at: string;
  confirmed_by: MonthCloseActor;
  financial_fingerprint: string | null;
  legacy_unverified: boolean;
  source: string;
  snapshot_summary: Record<string, unknown>;
  reopened: {
    reopened_at: string;
    reopened_by: MonthCloseActor | null;
    reason: string | null;
  } | null;
}

export interface MonthCloseHistoryPage extends ApiPage<MonthCloseRevision> {
  closure: MonthClosure;
  order: "newest" | "oldest";
}

export interface MonthCloseAsClosedReport {
  mode: "as_closed";
  period: Record<string, unknown>;
  revision_number: number;
  confirmed_at: string;
  confirmed_by: MonthCloseActor;
  legacy_unverified: boolean;
  financial_fingerprint: string | null;
  currencies: Array<Record<string, unknown>> | null;
  account_balances: Array<Record<string, unknown>> | null;
  category_aggregates: Array<Record<string, unknown>> | null;
  transaction_count: number | null;
  reconciliation_coverage: Array<Record<string, unknown>> | null;
  issue_summary: {
    blocker_count: number;
    warning_count: number;
    info_count: number;
    blockers: MonthCloseIssue[];
    warnings: MonthCloseIssue[];
    info: MonthCloseIssue[];
  } | null;
  unavailable_sections: string[];
}

export interface MonthCloseComparison {
  period_month: string;
  revision_number: number;
  as_closed: MonthCloseAsClosedReport;
  current: Record<string, unknown>;
  differences: {
    currencies: Array<Record<string, unknown>>;
    account_balances: Array<Record<string, unknown>>;
    category_aggregates: Array<Record<string, unknown>>;
  };
  unavailable_sections: string[];
}

export interface ApiPage<T> {
  items: T[];
  page: PageMeta;
}
