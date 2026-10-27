import assert from "node:assert/strict";
import test from "node:test";

import type { Account } from "@/types/finance";

import { accountArchiveMutation, accountDeleteMutation, accountMutation, accountRestoreDeletedMutation, initialAccountForm } from "./account-form";

const account: Account = {
  id: "11111111-1111-4111-8111-111111111111", name: "Карта", account_type: "credit_card", currency: "RUB",
  institution: "Банк", opening_balance: "100.0000", opening_balance_at: "2026-08-01T00:00:00.000Z",
  credit_limit: "50000.0000", description: null, is_archived: false, version: 7,
  created_at: "2026-08-01T00:00:00.000Z", updated_at: "2026-08-01T00:00:00.000Z",
};

test("account create includes opening date and credit limit", () => {
  const form = { ...initialAccountForm(new Date("2026-08-22T10:00:00.000Z")), accountType: "credit_card" as const, creditLimit: "50000.0000", name: "Карта" };
  const mutation = accountMutation(form, null);
  assert.equal(mutation.method, "POST");
  assert.equal(mutation.path, "/api/v1/accounts");
  assert.equal(mutation.body.credit_limit, "50000.0000");
  assert.match(mutation.body.opening_balance_at, /T/);
});

test("account edit and lifecycle mutations preserve optimistic version", () => {
  const edit = accountMutation({ ...initialAccountForm(), name: "Новая карта" }, account);
  assert.equal(edit.method, "PATCH");
  assert.equal(edit.body.version, 7);
  assert.deepEqual(accountArchiveMutation(account, true).body, { version: 7, is_archived: true });
  assert.equal(accountDeleteMutation(account).path, `/api/v1/accounts/${account.id}?version=7`);
  assert.deepEqual(accountRestoreDeletedMutation(account).body, { version: 7 });
});
