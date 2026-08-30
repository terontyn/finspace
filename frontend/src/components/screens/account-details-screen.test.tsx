import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { ApiClientError, apiClient } from "@/lib/api-client";
import type { Account, AccountBalance, Transaction } from "@/types/finance";

import { AccountDetailsScreen } from "./account-details-screen";

const account: Account = {
  account_type: "debit_card",
  created_at: "2026-08-01T00:00:00.000Z",
  credit_limit: null,
  currency: "RUB",
  description: "Основной счёт",
  id: "account-1",
  institution: "Банк",
  is_archived: false,
  name: "Основной",
  opening_balance: "100.0000",
  opening_balance_at: "2026-08-01T00:00:00.000Z",
  updated_at: "2026-08-20T00:00:00.000Z",
  version: 2,
};

const balance: AccountBalance = {
  account_id: account.id,
  balance: "125.0000",
  currency: account.currency,
  name: account.name,
  opening_balance: account.opening_balance,
};

const transaction: Transaction = {
  account: { id: account.id, name: account.name },
  amount: "25.0000",
  category: { id: "category-1", name: "Доход" },
  payee: null,
  comment: null,
  counterparty: "Работодатель",
  created_at: "2026-08-20T00:00:00.000Z",
  currency: "RUB",
  description: "Тестовая операция",
  external_id: null,
  id: "transaction-1",
  occurred_at: "2026-08-20T00:00:00.000Z",
  related_transaction_id: null,
  source: "manual",
  splits: [],
  status: "confirmed",
  target_account: null,
  transaction_type: "income",
  updated_at: "2026-08-20T00:00:00.000Z",
  version: 1,
};

function installBrowserGlobals(): () => void {
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const browserGlobal = {
    addEventListener: () => undefined,
    cancelIdleCallback: () => undefined,
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    confirm: () => true,
    location: { assign: () => undefined },
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

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
}

test("account details loads the account, exact balance, and account-scoped transactions", async () => {
  const originalGet = apiClient.get;
  const restoreBrowserGlobals = installBrowserGlobals();
  const requests: string[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    requests.push(path);
    if (path === `/api/v1/accounts/${account.id}`) return Promise.resolve(account as T);
    if (path === "/api/v1/accounts/balances") return Promise.resolve([balance] as T);
    if (path.startsWith("/api/v1/transactions?")) return Promise.resolve({ items: [transaction], page: { limit: 10, offset: 0, total: 1 } } as T);
    if (path.startsWith(`/api/v1/accounts/${account.id}/reconciliations?`)) return Promise.resolve({ items: [], page: { limit: 10, offset: 0, total: 0 } } as T);
    throw new Error(`Unexpected test request: ${path}`);
  }) as typeof apiClient.get;

  try {
    await act(async () => { renderer = create(<AccountDetailsScreen accountId={account.id} onError={(error) => { throw error; }} timezone="UTC"/>); await settle(); });
    const output = JSON.stringify(renderer?.toJSON());
    assert.match(output, /Банк · Основной/);
    assert.match(output, /125/);
    assert.match(output, /Работодатель/);
    const transactionRequest = requests.find((path) => path.startsWith("/api/v1/transactions?"));
    assert.ok(transactionRequest);
    assert.equal(new URL(transactionRequest, "https://finspace.test").searchParams.get("account_id"), account.id);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restoreBrowserGlobals();
  }
});

test("account details renders a dedicated not-found state", async () => {
  const originalGet = apiClient.get;
  const restoreBrowserGlobals = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    if (path === `/api/v1/accounts/${account.id}`) return Promise.reject(new ApiClientError("Account was not found", "ACCOUNT_NOT_FOUND", 404));
    if (path === "/api/v1/accounts/balances") return Promise.resolve([] as T);
    return Promise.resolve({ items: [], page: { limit: 10, offset: 0, total: 0 } } as T);
  }) as typeof apiClient.get;

  try {
    await act(async () => { renderer = create(<AccountDetailsScreen accountId={account.id} onError={() => undefined} timezone="UTC"/>); await settle(); });
    assert.match(JSON.stringify(renderer?.toJSON()), /Счёт не найден/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restoreBrowserGlobals();
  }
});

test("account details reports non-404 API failures and offers retry", async () => {
  const originalGet = apiClient.get;
  const restoreBrowserGlobals = installBrowserGlobals();
  const errors: unknown[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (() => Promise.reject(new ApiClientError("Backend failed", "INTERNAL_ERROR", 500))) as typeof apiClient.get;

  try {
    await act(async () => { renderer = create(<AccountDetailsScreen accountId={account.id} onError={(error) => errors.push(error)} timezone="UTC"/>); await settle(); });
    assert.equal(errors.length, 1);
    assert.match(JSON.stringify(renderer?.toJSON()), /Не удалось открыть счёт/);
    assert.ok(renderer?.root.findByProps({ children: "Повторить" }));
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restoreBrowserGlobals();
  }
});
