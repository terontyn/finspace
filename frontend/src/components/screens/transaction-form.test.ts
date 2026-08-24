import assert from "node:assert/strict";
import test from "node:test";

import type { Transaction } from "@/types/finance";

import { initialTransactionForm, transactionCancelMutation, transactionFormFromRecord, transactionMutation } from "./transaction-form";

const record: Transaction = {
  id: "tx-1", occurred_at: "2026-08-22T10:00:00.000Z", transaction_type: "expense", amount: "1250.50", currency: "RUB",
  account: { id: "account-1", name: "Основной" }, target_account: null, category: { id: "category-1", name: "Продукты" },
  counterparty: "Магазин", description: "Покупка", comment: "Чек", status: "confirmed", source: "manual",
  related_transaction_id: null, external_id: null, splits: [], version: 4, created_at: "2026-08-22T10:00:00Z", updated_at: "2026-08-22T10:00:00Z",
};

test("create mutation preserves production transaction fields", () => {
  const form = { ...initialTransactionForm(new Date("2026-08-22T10:00:00Z")), accountId: "account-1", amount: "1250.50", categoryId: "category-1", counterparty: "Магазин", comment: "Чек" };
  const mutation = transactionMutation(form, null);
  assert.equal(mutation.method, "POST");
  assert.equal(mutation.path, "/api/v1/transactions");
  assert.equal(mutation.body.amount, "1250.50");
  assert.equal(mutation.body.comment, "Чек");
});

test("edit mutation preserves optimistic version and splits", () => {
  const restored = transactionFormFromRecord({ ...record, splits: [{ id: "split-1", category_id: "category-1", category_name: "Продукты", amount: "1250.50" }] });
  assert.ok(restored);
  const mutation = transactionMutation(restored, record);
  assert.equal(mutation.method, "PATCH");
  assert.equal(mutation.path, "/api/v1/transactions/tx-1");
  assert.equal("version" in mutation.body ? mutation.body.version : null, 4);
  assert.deepEqual(mutation.body.splits, [{ category_id: "category-1", amount: "1250.50", comment: null }]);
});

test("cancel mutation uses the versioned production endpoint", () => {
  assert.deepEqual(transactionCancelMutation(record), { path: "/api/v1/transactions/tx-1/cancel", body: { version: 4 } });
});
