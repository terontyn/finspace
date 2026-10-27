import assert from "node:assert/strict";
import test from "node:test";

import { formatMoney, moneyTone } from "./money.ts";

test("money is formatted from its string representation", () => {
  const formatted = formatMoney("1234.5000", "RUB").replace(/[\u00a0\u202f]/g, " ");
  assert.match(formatted, /1 234,50/);
  assert.match(formatted, /₽/);
});

test("invalid money strings are displayed without numeric coercion", () => {
  assert.equal(formatMoney("not-money", "USD"), "not-money USD");
});

test("money tone follows the signed decimal string", () => {
  assert.equal(moneyTone("-1.0000"), "negative");
  assert.equal(moneyTone("0.0000"), "neutral");
  assert.equal(moneyTone("1.0000"), "positive");
});
