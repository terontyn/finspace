export type SyncMode = "push_only" | "bidirectional" | "paused";

export interface GoogleConnectionStatus {
  configured: boolean;
  connected: boolean;
  status: string | null;
  google_email: string | null;
  granted_scopes: string[];
  token_expires_at: string | null;
}

export interface GoogleSheetStatus {
  configured: boolean;
  provider: "apps_script_bridge" | "google_oauth";
  oauth_enabled: boolean;
  apps_script_bridge_enabled: boolean;
  public_backend_url: string | null;
  connection: GoogleConnectionStatus;
  binding_id: string | null;
  spreadsheet_id: string | null;
  spreadsheet_url: string | null;
  spreadsheet_name: string | null;
  template_version: number | null;
  status: string | null;
  sync_enabled: boolean;
  sync_mode: SyncMode | null;
  apps_script_enabled: boolean;
  last_successful_sync_at: string | null;
  last_reconciliation_at: string | null;
  pending_outbox: number;
  pending_inbox: number;
  failed_events: number;
  open_conflicts: number;
  last_error_code: string | null;
  last_error_message: string | null;
  webhook_configured: boolean;
  spreadsheet_registered: boolean;
  last_pull_at: string | null;
  last_ack_at: string | null;
  last_heartbeat_at: string | null;
  heartbeat_healthy: boolean;
}

export interface GoogleConnectResponse {
  authorization_url: string;
  expires_at: string;
}

export interface FullExportPreview {
  transactions: number;
  accounts: number;
  categories: number;
  pending_changes: number;
  open_conflicts: number;
  blocked: boolean;
  warning: string;
}

export interface AppsScriptSecret {
  binding_id: string;
  secret: string;
  webhook_url: string | null;
  secret_version: number;
  warning: string;
}

export interface AppsScriptBinding {
  id: string;
  provider: "apps_script_bridge";
  spreadsheet_id: string | null;
  spreadsheet_url: string | null;
  spreadsheet_name: string;
  template_version: number;
  status: string;
  sync_enabled: boolean;
  sync_mode: SyncMode;
  secret_created_at: string;
  secret_last_rotated_at: string | null;
  last_pull_at: string | null;
  last_ack_at: string | null;
  last_heartbeat_at: string | null;
  created_at: string;
}

export interface AppsScriptBindingSecret extends AppsScriptBinding {
  secret: string;
  backend_url: string;
  warning: string;
}

export interface AppsScriptPackage {
  files: Record<string, string>;
}

export interface SyncConflict {
  id: string;
  entity_type: string;
  entity_id: string;
  database_version: number;
  sheet_version: number | null;
  database_payload: Record<string, unknown>;
  sheet_payload: Record<string, unknown>;
  conflicting_fields: string[];
  status: string;
  resolution: string | null;
  resolved_payload: Record<string, unknown> | null;
  created_at: string;
  resolved_at: string | null;
}

export interface ConflictPage {
  items: SyncConflict[];
  page: { limit: number; offset: number; total: number };
}
