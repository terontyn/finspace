import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { apiClient } from "@/lib/api-client";
import type { ImportBatch, ImportRow } from "@/types/finance";

import { ImportScreen } from "./import-screen";

function batch(status = "ready"): ImportBatch {
  return {
    confirmed_at: null,
    created_at: "2026-08-23T10:00:00Z",
    detected_format: "csv-utf-8",
    file_sha256: "a".repeat(64),
    file_size: 1024,
    file_type: "csv",
    filename: "sample.csv",
    id: "batch-1",
    mapping: {
      fields: { account: "Account", amount: "Amount", date: "Date", transaction_type: "Type" },
      locale: "ru-RU",
    },
    rolled_back_at: null,
    status,
    summary: {
      accounts: ["Основной"],
      currencies: ["RUB"],
      date_from: "2026-08-05",
      date_to: "2026-08-05",
      duplicate: 1,
      invalid: 1,
      skipped: 1,
      source_columns: ["Date", "Type", "Amount", "Account"],
      total: 4,
      valid: 1,
    },
    updated_at: "2026-08-23T10:00:00Z",
  };
}

const rows: ImportRow[] = [{
  created_transaction_id: null,
  duplicate_transaction_id: "transaction-1",
  id: "row-1",
  normalized_data: null,
  raw_data: { Account: "Основной", Amount: "10", Date: "05.08.2026", Type: "Расход" },
  source_row_number: 2,
  source_sheet: null,
  status: "duplicate",
  validation_errors: null,
}];

function installBrowserGlobals(): () => void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: { confirm: () => true, location: { href: "https://finspace.test/import" } },
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
  await Promise.resolve();
}

function button(renderer: ReactTestRenderer, label: string) {
  const found = renderer.root.findAllByType("button").find((node) =>
    node.children.some((child) => typeof child === "string" && child.includes(label)),
  );
  assert.ok(found, `button ${label} not found`);
  return found;
}

test("import screen renders review states and commits one backend batch", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restoreBrowserGlobals = installBrowserGlobals();
  let current = batch();
  let renderer: ReactTestRenderer | undefined;
  const posts: Array<{ path: string; body: unknown; headers: unknown }> = [];
  apiClient.get = (<T,>(path: string) => {
    if (path.startsWith("/api/v1/imports?")) return Promise.resolve({ items: [current], page: { limit: 50, offset: 0, total: 1 } } as T);
    if (path.includes("/rows")) return Promise.resolve({ items: rows, page: { limit: 100, offset: 0, total: 1 } } as T);
    throw new Error(`Unexpected request: ${path}`);
  }) as typeof apiClient.get;
  apiClient.post = (<T,>(path: string, body?: unknown, headers?: Record<string, string>) => {
    posts.push({ path, body, headers });
    current = { ...current, confirmed_at: "2026-08-23T10:05:00Z", status: "imported" };
    return Promise.resolve({ affected_transactions: 1, batch: current } as T);
  }) as typeof apiClient.post;

  try {
    await act(async () => {
      renderer = create(<ImportScreen onError={(error) => { throw error; }} />);
      await settle();
    });
    await act(async () => {
      button(renderer!, "sample.csv").props.onClick();
      await settle();
    });
    const preview = JSON.stringify(renderer!.toJSON());
    assert.match(preview, /Дубликат/);
    assert.match(preview, /Совпадает с существующей/);
    assert.match(preview, /Валюты учитываются отдельно/);

    const checkboxes = renderer!.root.findAllByType("input").filter((node) => node.props.type === "checkbox");
    await act(async () => {
      checkboxes.at(-1)?.props.onChange({ target: { checked: true } });
    });
    await act(async () => {
      button(renderer!, "Импортировать batch").props.onClick();
      await settle();
    });
    assert.equal(posts.length, 1);
    assert.equal(posts[0].path, "/api/v1/imports/batch-1/commit");
    assert.deepEqual(posts[0].body, { confirm: true });
    assert.deepEqual(posts[0].headers, { "X-Idempotency-Key": "import-batch-1" });
    assert.match(JSON.stringify(renderer!.toJSON()), /Импорт завершён/);
    assert.match(JSON.stringify(renderer!.toJSON()), /Создано операций/);
    assert.match(JSON.stringify(renderer!.toJSON()), /Импортировано операций: 1/);
    assert.match(JSON.stringify(renderer!.toJSON()), /повторный commit недоступен/);
    assert.equal(
      renderer!.root.findAllByType("button").filter((node) =>
        node.children.some((child) => child === "Импортировать как новую"),
      ).length,
      0,
    );
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restoreBrowserGlobals();
  }
});

test("import screen reports commit failure without faking success", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restoreBrowserGlobals = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  const errors: unknown[] = [];
  apiClient.get = (<T,>(path: string) => {
    if (path.startsWith("/api/v1/imports?")) return Promise.resolve({ items: [batch()], page: { limit: 50, offset: 0, total: 1 } } as T);
    if (path.includes("/rows")) return Promise.resolve({ items: [], page: { limit: 100, offset: 0, total: 0 } } as T);
    throw new Error(`Unexpected request: ${path}`);
  }) as typeof apiClient.get;
  apiClient.post = (() => Promise.reject(new Error("commit failed"))) as typeof apiClient.post;

  try {
    await act(async () => {
      renderer = create(<ImportScreen onError={(error) => errors.push(error)} />);
      await settle();
    });
    await act(async () => {
      button(renderer!, "sample.csv").props.onClick();
      await settle();
    });
    const checkboxes = renderer!.root.findAllByType("input").filter((node) => node.props.type === "checkbox");
    await act(async () => checkboxes.at(-1)?.props.onChange({ target: { checked: true } }));
    await act(async () => {
      button(renderer!, "Импортировать batch").props.onClick();
      await settle();
    });
    assert.equal(errors.length, 1);
    assert.doesNotMatch(JSON.stringify(renderer!.toJSON()), /Импорт завершён/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restoreBrowserGlobals();
  }
});
