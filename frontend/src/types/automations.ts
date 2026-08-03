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

export interface MonthClosure {
  id: string;
  period_month: string;
  status: "draft" | "ready" | "blocked" | "confirmed" | "reopened";
  summary: Record<string, unknown>;
  blocking_issues: Array<Record<string, unknown>> | null;
  warning_issues: Array<Record<string, unknown>> | null;
  version: number;
  prepared_at: string | null;
  confirmed_at: string | null;
}

export interface ApiPage<T> {
  items: T[];
  page: PageMeta;
}
