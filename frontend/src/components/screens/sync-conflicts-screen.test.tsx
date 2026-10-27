import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { ApiClientError, apiClient } from "@/lib/api-client";
import type { SyncConflict } from "@/types/google";

import { SyncConflictsScreen } from "./sync-conflicts-screen";

const conflict: SyncConflict = {
  conflicting_fields: ["name", "is_archived"],
  created_at: "2026-08-23T10:00:00Z",
  database_payload: { is_archived: false, name: "Продукты" },
  database_version: 3,
  entity_id: "category-1",
  entity_type: "category",
  id: "conflict-1",
  resolution: null,
  resolved_at: null,
  resolved_payload: null,
  sheet_payload: { changed_fields: { is_archived: true, name: "Рестораны" } },
  sheet_version: 2,
  status: "open",
};

function installBrowserGlobals(): () => void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  Object.defineProperty(globalThis, "window", { configurable: true, value: { confirm: () => true } });
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
  await Promise.resolve();
}

function selectFirstConflict(renderer: ReactTestRenderer): void {
  const card = renderer.root.findAllByType("button").find((node) => String(node.props.className).startsWith("conflict-card"));
  assert.ok(card);
  card.props.onClick();
}

function resolutionButton(renderer: ReactTestRenderer, label: string) {
  const found = renderer.root.findAllByType("button").find((node) => node.children.includes(label));
  assert.ok(found, `button ${label} not found`);
  return found;
}

test("conflict screen shows field diff and resolves atomically through backend command", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restoreBrowserGlobals = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  let resolved = false;
  let posted: { path: string; body: unknown } | null = null;
  apiClient.get = (<T,>(path: string) => {
    assert.match(path, /status=open/);
    return Promise.resolve({ items: resolved ? [] : [conflict], page: { limit: 100, offset: 0, total: resolved ? 0 : 1 } } as T);
  }) as typeof apiClient.get;
  apiClient.post = (<T,>(path: string, body?: unknown) => {
    posted = { path, body };
    resolved = true;
    return Promise.resolve({ ...conflict, resolution: "keep_database", status: "resolved" } as T);
  }) as typeof apiClient.post;

  try {
    await act(async () => {
      renderer = create(<SyncConflictsScreen onError={(error) => { throw error; }} />);
      await settle();
    });
    await act(async () => selectFirstConflict(renderer!));
    const output = JSON.stringify(renderer!.toJSON());
    assert.match(output, /Название/);
    assert.match(output, /Продукты/);
    assert.match(output, /Рестораны/);
    assert.match(output, /Finspace v/);
    assert.match(output, /Google Sheets v/);

    await act(async () => {
      resolutionButton(renderer!, "Оставить Finspace").props.onClick();
      await settle();
    });
    assert.deepEqual(posted, {
      body: { resolution: "keep_database" },
      path: "/api/v1/google-sheets/conflicts/conflict-1/resolve",
    });
    assert.match(JSON.stringify(renderer!.toJSON()), /Всё синхронизировано/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restoreBrowserGlobals();
  }
});

test("stale 409 remains visible and is handed to the shared error UX", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restoreBrowserGlobals = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  const errors: unknown[] = [];
  apiClient.get = (<T,>() => Promise.resolve({ items: [conflict], page: { limit: 100, offset: 0, total: 1 } } as T)) as typeof apiClient.get;
  apiClient.post = (() => Promise.reject(new ApiClientError(
    "Сущность изменилась после создания конфликта",
    "GOOGLE_SYNC_CONFLICT_STALE",
    409,
  ))) as typeof apiClient.post;

  try {
    await act(async () => {
      renderer = create(<SyncConflictsScreen onError={(error) => errors.push(error)} />);
      await settle();
    });
    await act(async () => selectFirstConflict(renderer!));
    await act(async () => {
      resolutionButton(renderer!, "Принять Google").props.onClick();
      await settle();
    });
    assert.equal(errors.length, 1);
    assert.ok(errors[0] instanceof ApiClientError);
    assert.equal((errors[0] as ApiClientError).status, 409);
    assert.match(JSON.stringify(renderer!.toJSON()), /Рестораны/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restoreBrowserGlobals();
  }
});
