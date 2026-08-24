import assert from "node:assert/strict";
import test from "node:test";

import {
  conflictDiff,
  conflictFieldDiffs,
  conflictResolutionMessage,
  fullExportMessage,
  googleStateLabel,
  parseMergedPayload,
} from "./google-sync.ts";
import type { GoogleSheetStatus, SyncConflict } from "../types/google.ts";

function status(overrides: Partial<GoogleSheetStatus> = {}): GoogleSheetStatus {
  return {
    configured: true,
    provider: "apps_script_bridge",
    oauth_enabled: false,
    apps_script_bridge_enabled: true,
    public_backend_url: "https://finspace.example",
    connection: {
      configured: false,
      connected: false,
      status: null,
      google_email: null,
      granted_scopes: [],
      token_expires_at: null,
    },
    binding_id: "binding",
    spreadsheet_id: "sheet-1",
    spreadsheet_url: null,
    spreadsheet_name: null,
    template_version: 1,
    status: "active",
    sync_enabled: true,
    sync_mode: "bidirectional",
    apps_script_enabled: true,
    last_successful_sync_at: null,
    last_reconciliation_at: null,
    pending_outbox: 0,
    pending_inbox: 0,
    failed_events: 0,
    open_conflicts: 0,
    last_error_code: null,
    last_error_message: null,
    webhook_configured: true,
    spreadsheet_registered: true,
    last_pull_at: null,
    last_ack_at: null,
    last_heartbeat_at: "2026-07-22T10:00:00Z",
    heartbeat_healthy: true,
    ...overrides,
  };
}

test("provider not configured state", () => {
  assert.equal(googleStateLabel(status({ configured: false })), "Провайдер не настроен");
});

test("bridge waits for registration", () => {
  assert.equal(
    googleStateLabel(status({ spreadsheet_registered: false })),
    "Ожидает регистрации таблицы",
  );
});

test("revoked OAuth state remains visible", () => {
  const value = status({
    provider: "google_oauth",
    oauth_enabled: true,
    connection: { ...status().connection, connected: false, status: "revoked" },
  });
  assert.equal(googleStateLabel(value), "Доступ Google отозван");
});

test("binding creation state", () => {
  assert.equal(googleStateLabel(status({ binding_id: null })), "Binding не создан");
});

test("OAuth push-only state", () => {
  const value = status({
    provider: "google_oauth",
    oauth_enabled: true,
    sync_mode: "push_only",
    connection: { ...status().connection, configured: true, connected: true, status: "active" },
  });
  assert.equal(googleStateLabel(value), "Только PostgreSQL → Sheets");
});

test("active bidirectional state", () => {
  assert.equal(googleStateLabel(status()), "Двусторонняя синхронизация");
});

test("heartbeat, paused and error states", () => {
  assert.equal(
    googleStateLabel(status({ heartbeat_healthy: false })),
    "Ожидает heartbeat",
  );
  assert.equal(
    googleStateLabel(status({ status: "paused" })),
    "Синхронизация приостановлена",
  );
  assert.equal(googleStateLabel(status({ status: "error" })), "Ошибка синхронизации");
});

test("conflict diff keeps both payloads", () => {
  const conflict = {
    database_payload: { amount: "10" },
    sheet_payload: { amount: "11" },
  } as unknown as SyncConflict;
  const diff = conflictDiff(conflict);
  assert.match(diff.database, /10/);
  assert.match(diff.sheet, /11/);
});

test("conflict field diff prefers external changed fields over technical payload", () => {
  const conflict = {
    conflicting_fields: ["name", "is_archived"],
    database_payload: { name: "Продукты", is_archived: false },
    sheet_payload: {
      changed_fields: { name: "Рестораны", is_archived: true },
      visible_row: { name: "Не использовать" },
    },
  } as unknown as SyncConflict;
  assert.deepEqual(conflictFieldDiffs(conflict), [
    { field: "name", label: "Название", database: "Продукты", external: "Рестораны" },
    { field: "is_archived", label: "Архив", database: false, external: true },
  ]);
});

test("manual conflict resolution validates a JSON object", () => {
  assert.deepEqual(parseMergedPayload('{"amount":"12.50"}'), { amount: "12.50" });
  assert.throws(() => parseMergedPayload("[]"), /JSON-объектом/);
  assert.match(conflictResolutionMessage("manual_merge"), /ручное объединение/);
});

test("full export warning includes counts and non-deletion guarantee", () => {
  const message = fullExportMessage({
    transactions: 7,
    accounts: 2,
    categories: 3,
    pending_changes: 1,
    open_conflicts: 4,
    blocked: true,
    warning: "Нужен force",
  });
  assert.match(message, /Операции: 7/);
  assert.match(message, /DIRTY: 1/);
  assert.match(message, /удалены не будут/);
});
