import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { apiClient } from "@/lib/api-client";
import type { GoogleSheetStatus } from "@/types/google";

import { GoogleSheetsScreen } from "./google-sheets-screen";

function connectedStatus(overrides: Partial<GoogleSheetStatus> = {}): GoogleSheetStatus {
  return {
    apps_script_bridge_enabled: true,
    apps_script_enabled: true,
    binding_id: "binding-1",
    configured: true,
    connection: {
      configured: false,
      connected: false,
      google_email: null,
      granted_scopes: [],
      status: null,
      token_expires_at: null,
    },
    failed_events: 0,
    heartbeat_healthy: true,
    last_ack_at: "2026-08-23T10:01:00Z",
    last_error_code: null,
    last_error_message: null,
    last_heartbeat_at: "2026-08-23T10:02:00Z",
    last_pull_at: "2026-08-23T10:00:00Z",
    last_reconciliation_at: null,
    last_successful_sync_at: "2026-08-23T10:01:00Z",
    oauth_enabled: false,
    open_conflicts: 2,
    pending_inbox: 1,
    pending_outbox: 3,
    provider: "apps_script_bridge",
    public_backend_url: "https://api.finspace.test",
    spreadsheet_id: "workbook-1",
    spreadsheet_name: "Финпространство — тест",
    spreadsheet_registered: true,
    spreadsheet_url: "https://docs.google.com/spreadsheets/d/workbook-1",
    status: "active",
    sync_enabled: true,
    sync_mode: "bidirectional",
    template_version: 1,
    webhook_configured: true,
    ...overrides,
  };
}

function installBrowserGlobals(): () => void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: { confirm: () => true, location: { assign: () => undefined } },
  });
  Object.defineProperty(globalThis, "self", { configurable: true, value: globalThis });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });
  return () => {
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
    else Reflect.deleteProperty(globalThis, "window");
    if (originalSelf) Object.defineProperty(globalThis, "self", originalSelf);
    else Reflect.deleteProperty(globalThis, "self");
    Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
  };
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

test("Google Sheets screen leads with real product status and keeps internals in diagnostics", async () => {
  const originalGet = apiClient.get;
  const restoreBrowserGlobals = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  let requests = 0;
  apiClient.get = (<T,>(path: string) => {
    assert.equal(path, "/api/v1/google-sheets/status");
    requests += 1;
    return Promise.resolve(connectedStatus() as T);
  }) as typeof apiClient.get;

  try {
    await act(async () => {
      renderer = create(<GoogleSheetsScreen onError={(error) => { throw error; }} />);
      await settle();
    });
    const output = JSON.stringify(renderer!.toJSON());
    assert.match(output, /Финпространство — тест/);
    assert.match(output, /Подключено/);
    assert.match(output, /Последняя успешная синхронизация/);
    assert.match(output, /Разрешить конфликты/);
    assert.match(output, /Диагностика/);
    assert.match(output, /Outbox \/ Inbox/);
    assert.match(output, /workbook-1/);
    assert.doesNotMatch(output, /HMAC/);

    const refresh = renderer!.root.findAllByType("button").find((node) => node.children.includes("Обновить состояние"));
    assert.ok(refresh);
    await act(async () => {
      refresh.props.onClick();
      await settle();
    });
    assert.equal(requests, 2);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restoreBrowserGlobals();
  }
});

test("Google Sheets disconnected state offers only the real setup flow", async () => {
  const originalGet = apiClient.get;
  const restoreBrowserGlobals = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>() => Promise.resolve(connectedStatus({
    binding_id: null,
    heartbeat_healthy: false,
    spreadsheet_id: null,
    spreadsheet_name: null,
    spreadsheet_registered: false,
    spreadsheet_url: null,
    status: null,
  }) as T)) as typeof apiClient.get;

  try {
    await act(async () => {
      renderer = create(<GoogleSheetsScreen onError={(error) => { throw error; }} />);
      await settle();
    });
    const output = JSON.stringify(renderer!.toJSON());
    assert.match(output, /Не подключено/);
    assert.match(output, /Первое подключение/);
    assert.match(output, /Создать подключение/);
    assert.doesNotMatch(output, /Повторный full export/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restoreBrowserGlobals();
  }
});
