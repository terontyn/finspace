import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { apiClient } from "@/lib/api-client";
import type { Transaction } from "@/types/finance";

import { TodayScreen } from "./today-screen";

const draftTransaction: Transaction = {
  account: { id: "account-1", name: "Основной" },
  amount: "25.0000",
  category: { id: "category-1", name: "Покупки" },
  comment: null,
  counterparty: "Черновая покупка",
  created_at: "2026-08-23T08:00:00.000Z",
  currency: "RUB",
  description: null,
  external_id: null,
  id: "transaction-1",
  occurred_at: "2026-08-23T08:00:00.000Z",
  related_transaction_id: null,
  source: "manual",
  splits: [],
  status: "draft",
  target_account: null,
  transaction_type: "expense",
  updated_at: "2026-08-23T08:00:00.000Z",
  version: 1,
};

function installBrowserGlobals(): () => void {
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const browserGlobal = {
    addEventListener: () => undefined,
    cancelIdleCallback: () => undefined,
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    removeEventListener: () => undefined,
    requestIdleCallback: (callback: () => void) => { callback(); return 1; },
    setTimeout: globalThis.setTimeout.bind(globalThis),
  };
  Object.defineProperty(globalThis, "self", { configurable: true, value: browserGlobal });
  Object.defineProperty(globalThis, "window", { configurable: true, value: browserGlobal });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });
  return () => {
    if (originalSelf) Object.defineProperty(globalThis, "self", originalSelf);
    else Reflect.deleteProperty(globalThis, "self");
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
    else Reflect.deleteProperty(globalThis, "window");
    Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
  };
}

test("dashboard labels non-posted recent transactions with their real status", async () => {
  const originalGet = apiClient.get;
  const restoreBrowserGlobals = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    if (path === "/api/v1/accounts/balances") return Promise.resolve([] as T);
    if (path.startsWith("/api/v1/financial-summary?")) return Promise.resolve({ groups: [] } as T);
    if (path === "/api/v1/transactions?limit=6&offset=0") {
      return Promise.resolve({ items: [draftTransaction], page: { limit: 6, offset: 0, total: 1 } } as T);
    }
    throw new Error(`Unexpected test request: ${path}`);
  }) as typeof apiClient.get;

  try {
    await act(async () => {
      renderer = create(<TodayScreen onError={(error) => { throw error; }} timezone="UTC" />);
      await Promise.resolve();
      await Promise.resolve();
    });

    const recentMetadata = renderer?.root.findAllByType("small").find((node) =>
      node.children.some((child) => typeof child === "string" && child.includes("Черновик")),
    );
    assert.ok(recentMetadata, "the recent transaction must expose its draft status");
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restoreBrowserGlobals();
  }
});
