import assert from "node:assert/strict";
import test from "node:test";

import type { AccountBalance } from "@/types/finance";

import { currentMonthPeriod, financialSummaryUrl, groupBalanceTotals } from "./dashboard-data";

test("dashboard requests the real financial summary for the current month", () => {
  const reference = new Date("2026-08-22T10:00:00.000Z");
  const period = currentMonthPeriod(reference, "Asia/Yekaterinburg");
  const url = new URL(financialSummaryUrl(period), "https://finspace.test");

  assert.equal(period.dateFrom, "2026-07-31T19:00:00.000Z");
  assert.equal(url.pathname, "/api/v1/financial-summary");
  assert.equal(url.searchParams.get("date_from"), period.dateFrom);
  assert.equal(url.searchParams.get("date_to"), reference.toISOString());
});

test("dashboard totals exact backend balances without mixing currencies", () => {
  const balances: AccountBalance[] = [
    { account_id: "1", name: "Основной", currency: "RUB", opening_balance: "0.0000", balance: "10.1250" },
    { account_id: "2", name: "Наличные", currency: "RUB", opening_balance: "0.0000", balance: "-0.1250" },
    { account_id: "3", name: "Travel", currency: "EUR", opening_balance: "0.0000", balance: "3.5000" },
  ];

  assert.deepEqual(groupBalanceTotals(balances), [
    { accountsCount: 1, currency: "EUR", total: "3.5000" },
    { accountsCount: 2, currency: "RUB", total: "10.0000" },
  ]);
});
