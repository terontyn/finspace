import assert from "node:assert/strict";
import test from "node:test";

import {
  importStatusLabel,
  importStep,
  mappingMissing,
  rowErrors,
  rowSource,
} from "./import-workflow.ts";
import type { ImportBatch, ImportRow } from "../../types/finance.ts";

test("import mapping requires type only for a shared amount column", () => {
  assert.deepEqual(mappingMissing({ date: "Date", account: "Account", amount: "Amount" }), [
    "тип операции",
  ]);
  assert.deepEqual(mappingMissing({
    account: "Account",
    date: "Date",
    income_amount: "Income",
    expense_amount: "Expense",
  }), []);
});

test("import workflow follows the real backend lifecycle", () => {
  const batch = { status: "mapping_required" } as ImportBatch;
  assert.equal(importStep(null), 1);
  assert.equal(importStep(batch), 2);
  assert.equal(importStep({ ...batch, status: "ready" }), 3);
  assert.equal(importStep({ ...batch, status: "imported" }), 4);
  assert.equal(importStatusLabel("duplicate"), "Дубликат");
});

test("review presenter explains invalid, duplicate and empty rows", () => {
  const row: ImportRow = {
    created_transaction_id: null,
    duplicate_transaction_id: null,
    id: "row-1",
    normalized_data: null,
    raw_data: { Date: "", Amount: "" },
    source_row_number: 2,
    source_sheet: null,
    status: "skipped",
    validation_errors: null,
  };
  assert.equal(rowSource(row), "Пустая строка");
  assert.equal(rowErrors({ ...row, status: "duplicate" }), "Совпадает с существующей или другой строкой файла");
  assert.equal(rowErrors({
    ...row,
    status: "invalid",
    validation_errors: [{ code: "VALIDATION_ERROR", message: "Amount is invalid" }],
  }), "Строка не прошла проверку");
  assert.equal(rowErrors({
    ...row,
    status: "invalid",
    validation_errors: [{ code: "VALIDATION_ERROR", message: "Amount is not a decimal number" }],
  }), "Сумма указана не в числовом формате");
  assert.equal(rowErrors({
    ...row,
    status: "invalid",
    validation_errors: [{ code: "VALIDATION_ERROR", message: "Unknown account: Карта" }],
  }), "Счёт не найден: Карта");
});
