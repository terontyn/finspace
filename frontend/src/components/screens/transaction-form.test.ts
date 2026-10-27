import assert from "node:assert/strict";
import test from "node:test";

import type { Transaction } from "@/types/finance";

import { initialTransactionForm, transactionCancelMutation, transactionFormFromRecord, transactionFormWithCounterparty, transactionFormWithPayee, transactionMutation } from "./transaction-form";

const record: Transaction = {
  id: "tx-1", occurred_at: "2026-08-22T10:00:00.000Z", transaction_type: "expense", amount: "1250.50", currency: "RUB",
  account: { id: "account-1", name: "Основной" }, target_account: null, category: { id: "category-1", name: "Продукты" },
  payee: null,
  counterparty: "Магазин", description: "Покупка", comment: "Чек", status: "confirmed", source: "manual",
  related_transaction_id: null, external_id: null, splits: [], version: 4, created_at: "2026-08-22T10:00:00Z", updated_at: "2026-08-22T10:00:00Z",
};

test("create mutation preserves production transaction fields", () => {
  const initial = initialTransactionForm(new Date("2026-08-22T10:00:00Z"));
  assert.equal(initial.payeeId, "");
  const form = { ...initial, accountId: "account-1", amount: "1250.50", categoryId: "category-1", counterparty: "Магазин", comment: "Чек", payeeId: "payee-1" };
  const mutation = transactionMutation(form, null);
  assert.equal(mutation.method, "POST");
  assert.equal(mutation.path, "/api/v1/transactions");
  assert.equal(mutation.body.amount, "1250.50");
  assert.equal(mutation.body.comment, "Чек");
  assert.equal(mutation.body.payee_id, "payee-1");
});

test("edit mutation preserves optimistic version and splits", () => {
  const assigned = { id: "payee-1", name: "Кофейня" };
  const restored = transactionFormFromRecord({ ...record, payee: assigned, splits: [{ id: "split-1", category_id: "category-1", category_name: "Продукты", amount: "1250.50" }] });
  assert.ok(restored);
  assert.equal(restored.payeeId, "payee-1");
  const mutation = transactionMutation(restored, record);
  assert.equal(mutation.method, "PATCH");
  assert.equal(mutation.path, "/api/v1/transactions/tx-1");
  assert.equal("version" in mutation.body ? mutation.body.version : null, 4);
  assert.deepEqual(mutation.body.splits, [{ category_id: "category-1", amount: "1250.50", comment: null }]);
  assert.equal(mutation.body.payee_id, "payee-1");
});

test("clearing Payee serializes null independently from raw counterparty", () => {
  const form = { ...initialTransactionForm(), accountId: "account-1", amount: "10.00", counterparty: "RAW SHOP", payeeId: "payee-1" };
  const withoutPayee = transactionFormWithPayee(form, "");
  assert.equal(withoutPayee.counterparty, "RAW SHOP");
  assert.equal(transactionMutation(withoutPayee, record).body.payee_id, null);

  const changedCounterparty = transactionFormWithCounterparty(form, "OTHER RAW VALUE");
  assert.equal(changedCounterparty.payeeId, "payee-1");
  assert.equal(transactionMutation(changedCounterparty, null).body.payee_id, "payee-1");

  const selectedPayee = transactionFormWithPayee({ ...form, counterparty: "UNCHANGED" }, "payee-2");
  assert.equal(selectedPayee.counterparty, "UNCHANGED");
  assert.equal(transactionMutation(selectedPayee, null).body.counterparty, "UNCHANGED");
});

test("cancel mutation uses the versioned production endpoint", () => {
  assert.deepEqual(transactionCancelMutation(record), { path: "/api/v1/transactions/tx-1/cancel", body: { version: 4 } });
});
