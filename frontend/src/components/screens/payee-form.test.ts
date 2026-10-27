import assert from "node:assert/strict";
import test from "node:test";

import type { Payee } from "@/types/finance";

import {
  initialPayeeForm,
  payeeAliasArchiveMutation,
  payeeAliasCreateMutation,
  payeeAliasRestoreMutation,
  payeeArchiveMutation,
  payeeFormFromRecord,
  payeeMutation,
  payeeRestoreMutation,
} from "./payee-form";

const payee: Payee = {
  aliases: [
    { id: "alias-primary", alias: "Магазин", is_primary: true, created_at: "2026-08-30T00:00:00Z", updated_at: "2026-08-30T00:00:00Z", deleted_at: null },
    { id: "alias-secondary", alias: "SHOP", is_primary: false, created_at: "2026-08-30T00:00:00Z", updated_at: "2026-08-30T00:00:00Z", deleted_at: null },
  ],
  created_at: "2026-08-30T00:00:00Z",
  deleted_at: null,
  id: "payee-1",
  name: "Магазин",
  notes: "Продукты",
  updated_at: "2026-08-30T00:00:00Z",
  version: 7,
};

test("payee form maps records and create/edit payloads", () => {
  assert.deepEqual(initialPayeeForm(), { name: "", notes: "" });
  assert.deepEqual(payeeFormFromRecord(payee), { name: "Магазин", notes: "Продукты" });
  assert.deepEqual(payeeMutation({ name: "  Новый  ", notes: "  " }, null), {
    method: "POST",
    path: "/api/v1/payees",
    body: { name: "Новый", notes: null },
  });
  assert.deepEqual(payeeMutation({ name: "Магазин 2", notes: "Заметка" }, payee), {
    method: "PATCH",
    path: "/api/v1/payees/payee-1",
    body: { name: "Магазин 2", notes: "Заметка", version: 7 },
  });
});

test("payee and alias lifecycle mutations preserve optimistic versions", () => {
  const alias = payee.aliases[1];
  assert.deepEqual(payeeArchiveMutation(payee), { path: "/api/v1/payees/payee-1?version=7" });
  assert.deepEqual(payeeRestoreMutation(payee), { path: "/api/v1/payees/payee-1/restore", body: { version: 7 } });
  assert.deepEqual(payeeAliasCreateMutation(payee, "  SHOP 2  "), {
    path: "/api/v1/payees/payee-1/aliases",
    body: { alias: "SHOP 2", version: 7 },
  });
  assert.deepEqual(payeeAliasArchiveMutation(payee, alias), {
    path: "/api/v1/payees/payee-1/aliases/alias-secondary?version=7",
  });
  assert.deepEqual(payeeAliasRestoreMutation(payee, alias), {
    path: "/api/v1/payees/payee-1/aliases/alias-secondary/restore",
    body: { version: 7 },
  });
});
