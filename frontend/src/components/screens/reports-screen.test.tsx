import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { apiClient } from "@/lib/api-client";
import type { FinancialReport } from "@/types/finance";

import { ReportsScreen } from "./reports-screen";

const report: FinancialReport = {
  period: {
    cutoff_from: "2026-07-31T19:00:00Z",
    cutoff_to: "2026-08-31T19:00:00Z",
    date_from: "2026-08-01",
    date_to: "2026-08-31",
    timezone: "Asia/Yekaterinburg",
  },
  groups: [{
    adjustment: "0.0000",
    currency: "RUB",
    expense: "100.0000",
    income: "250.0000",
    largest_expenses: [{
      account_id: "account-1",
      account_name: "Основной",
      amount: "100.0000",
      category_name: "Продукты",
      counterparty: "Магазин",
      description: null,
      occurred_at: "2026-08-10T12:00:00Z",
      transaction_id: "transaction-1",
    }],
    monthly_comparison: [
      { adjustment: "0.0000", expense: "80.0000", income: "200.0000", month: "2026-07", net_cashflow: "120.0000", transactions_count: 2 },
      { adjustment: "0.0000", expense: "100.0000", income: "250.0000", month: "2026-08", net_cashflow: "150.0000", transactions_count: 3 },
    ],
    net_cashflow: "150.0000",
    spending_by_category: [{ amount: "100.0000", category_id: "category-1", name: "Продукты", transaction_count: 1 }],
    transactions_count: 3,
    transfer_volume: "50.0000",
  }],
};

function installBrowserGlobals(): () => void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  const originalActEnvironment = Object.getOwnPropertyDescriptor(
    globalThis,
    "IS_REACT_ACT_ENVIRONMENT",
  );
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      history: { replaceState: () => undefined },
      location: {
        hash: "",
        href: "https://finspace.test/reports?period=2026-08&currency=RUB",
        pathname: "/reports",
        search: "?period=2026-08&currency=RUB",
      },
    },
  });
  Object.defineProperty(globalThis, "self", {
    configurable: true,
    value: globalThis,
  });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", {
    configurable: true,
    value: true,
  });
  return () => {
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
    else Reflect.deleteProperty(globalThis, "window");
    if (originalSelf) Object.defineProperty(globalThis, "self", originalSelf);
    else Reflect.deleteProperty(globalThis, "self");
    if (originalActEnvironment) {
      Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", originalActEnvironment);
    } else {
      Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
    }
  };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

test("reports screen renders backend values and keeps transfer outside cash flow", async () => {
  const originalGet = apiClient.get;
  const restoreBrowserGlobals = installBrowserGlobals();
  const requests: string[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    requests.push(path);
    return Promise.resolve(report as T);
  }) as typeof apiClient.get;

  try {
    await act(async () => {
      renderer = create(
        <ReportsScreen onError={(error) => { throw error; }} timezone="Asia/Yekaterinburg"/>,
      );
      await settle();
    });
    assert.equal(requests.length, 1);
    const url = new URL(requests[0], "https://finspace.test");
    assert.equal(url.pathname, "/api/v1/reports/financial");
    assert.equal(url.searchParams.get("date_from"), "2026-08-01");
    assert.equal(url.searchParams.get("date_to"), "2026-08-31");
    assert.equal(url.searchParams.get("currency"), "RUB");
    const output = JSON.stringify(renderer?.toJSON());
    assert.match(output, /250/);
    assert.match(output, /100/);
    assert.match(output, /150/);
    assert.match(output, /Переводы/);
    assert.match(output, /50/);
    assert.match(output, /Продукты/);
    assert.match(output, /Магазин/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restoreBrowserGlobals();
  }
});
