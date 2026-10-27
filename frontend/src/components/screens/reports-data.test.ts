import assert from "node:assert/strict";
import test from "node:test";

import {
  financialReportUrl,
  parseReportQuery,
  reportPeriodFromMonth,
} from "./reports-data";

test("report month produces an inclusive calendar period without money calculations", () => {
  assert.deepEqual(reportPeriodFromMonth("2024-02"), {
    dateFrom: "2024-02-01",
    dateTo: "2024-02-29",
  });
  assert.deepEqual(reportPeriodFromMonth("2026-08"), {
    dateFrom: "2026-08-01",
    dateTo: "2026-08-31",
  });
});

test("report API URL preserves optional currency separation", () => {
  const all = new URL(financialReportUrl("2026-08", "ALL"), "https://finspace.test");
  const rub = new URL(financialReportUrl("2026-08", "RUB"), "https://finspace.test");
  assert.equal(all.pathname, "/api/v1/reports/financial");
  assert.equal(all.searchParams.get("currency"), null);
  assert.equal(rub.searchParams.get("currency"), "RUB");
  assert.equal(rub.searchParams.get("date_from"), "2026-08-01");
  assert.equal(rub.searchParams.get("date_to"), "2026-08-31");
});

test("report URL state accepts only supported month and currency values", () => {
  assert.deepEqual(parseReportQuery("?period=2026-08&currency=USD", "2026-01"), {
    month: "2026-08",
    currency: "USD",
  });
  assert.deepEqual(parseReportQuery("?period=bad&currency=BTC", "2026-01"), {
    month: "2026-01",
    currency: "ALL",
  });
});
