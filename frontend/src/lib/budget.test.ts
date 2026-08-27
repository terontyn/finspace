import assert from "node:assert/strict";
import test from "node:test";

import {
  allowedBudgetCategories,
  budgetErrorMessage,
  budgetFormFromGroup,
  budgetRequestFromForm,
  currentBudgetPeriod,
  MutationIdentity,
  shiftBudgetPeriod,
  validateBudgetForm,
} from "./budget";
import { ApiClientError } from "./api-client";
import type { BudgetGroup } from "@/types/budget";
import type { Category } from "@/types/finance";

const group: BudgetGroup = {
  actual_expense: "40.0000",
  actual_income: "100.0000",
  actual_net_cashflow: "60.0000",
  adjustment: "0.0000",
  allocated: "80.0000",
  allocations: [{
    actual: "40.0000",
    category_archived: false,
    category_deleted: false,
    category_id: "category-1",
    category_name: "Продукты",
    category_type: "expense",
    id: "allocation-1",
    note: "Базовый план",
    parent_id: null,
    planned: "80.0000",
    remaining: "40.0000",
    usage_percent: "50.0000",
  }],
  budgeted_actual_expense: "40.0000",
  currency: "RUB",
  deleted_at: null,
  frozen: false,
  id: "budget-1",
  period: "2026-08",
  planned_income: "100.0000",
  planning_capacity: "110.0000",
  projection_source: "live",
  remaining: "70.0000",
  rollover: { amount: "10.0000", provisional: true, source_policy: "full" },
  rollover_policy: "positive_only",
  unallocated: "30.0000",
  unbudgeted_actual_expense: "0.0000",
  version: 4,
  workspace_id: "workspace-1",
};

test("Budget calendar helpers cross year boundaries and use workspace timezone", () => {
  assert.equal(shiftBudgetPeriod("2026-01", -1), "2025-12");
  assert.equal(shiftBudgetPeriod("2026-12", 1), "2027-01");
  const instant = new Date("2026-08-31T21:30:00Z");
  assert.equal(currentBudgetPeriod("Asia/Yekaterinburg", instant), "2026-09");
  assert.equal(currentBudgetPeriod("Europe/London", instant), "2026-08");
});

test("one mutation identity survives retry and reset creates a new command", () => {
  const identity = new MutationIdentity();
  const first = identity.current();
  assert.equal(identity.current(), first);
  identity.reset();
  assert.notEqual(identity.current(), first);
});

test("Budget form sends version and the complete exact-category collection", () => {
  const form = budgetFormFromGroup(group);
  form.plannedIncome = "125,50";
  form.allocations.push({ categoryId: "category-child", note: " child ", plannedAmount: "20,25" });
  assert.deepEqual(validateBudgetForm(form), {});
  assert.deepEqual(budgetRequestFromForm(form, group.version), {
    allocations: [
      { category_id: "category-1", note: "Базовый план", planned_amount: "80.0000" },
      { category_id: "category-child", note: "child", planned_amount: "20.25" },
    ],
    planned_income: "125.50",
    rollover_policy: "positive_only",
    version: 4,
  });
});

test("new allocations only offer active expense/both categories", () => {
  const base = { color: null, created_at: "2026-01-01T00:00:00Z", icon: null, parent_id: null, sort_order: 0, updated_at: "2026-01-01T00:00:00Z", version: 1 };
  const categories: Category[] = [
    { ...base, category_type: "expense", id: "expense", is_archived: false, name: "Expense" },
    { ...base, category_type: "both", id: "both", is_archived: false, name: "Both" },
    { ...base, category_type: "income", id: "income", is_archived: false, name: "Income" },
    { ...base, category_type: "expense", id: "archived", is_archived: true, name: "Archived" },
  ];
  assert.deepEqual(allowedBudgetCategories(categories).map((item) => item.id), ["expense", "both"]);
});

test("Budget form rejects duplicate exact categories and invalid money", () => {
  const form = budgetFormFromGroup(group);
  form.allocations.push({ categoryId: "category-1", note: "", plannedAmount: "0" });
  const errors = validateBudgetForm(form);
  assert.match(errors.allocations ?? "", /положительную|один раз/);
  form.plannedIncome = "-1";
  assert.match(validateBudgetForm(form).plannedIncome ?? "", /неотрицательной/);
});

test("Budget error fallback never renders structured objects or raw JSON", () => {
  assert.equal(
    budgetErrorMessage(new ApiClientError("[object Object]", "UNKNOWN_BUDGET_ERROR", 500)),
    "Backend отклонил команду бюджета.",
  );
  assert.equal(
    budgetErrorMessage(new ApiClientError('{"secret":"must-not-render"}', "UNKNOWN_BUDGET_ERROR", 500)),
    "Backend отклонил команду бюджета.",
  );
  assert.match(
    budgetErrorMessage(new ApiClientError("Readable backend detail", "UNKNOWN_BUDGET_ERROR", 500)),
    /Readable backend detail/,
  );
});
