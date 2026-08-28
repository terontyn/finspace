import assert from "node:assert/strict";
import test from "node:test";

import { ApiClientError } from "./api-client";
import {
  goalCreateRequest,
  goalDeadlineLabel,
  goalErrorMessage,
  formatGoalTargetDate,
  goalProgressPresentation,
  goalUpdateRequest,
  GoalMutationIdentity,
  initialGoalForm,
  validateGoalForm,
} from "./goals";
import type { Goal } from "@/types/goals";

const goal: Goal = {
  contributed_amount: "125.0000",
  contribution_count: 2,
  created_at: "2026-08-01T00:00:00Z",
  created_by: "user-1",
  currency: "RUB",
  days_remaining: -2,
  deleted_at: null,
  description: "Резерв",
  id: "goal-1",
  is_target_reached: true,
  name: "Подушка",
  overdue: true,
  progress_percent: "125.0000",
  remaining_amount: "-25.0000",
  status: "active",
  target_amount: "100.0000",
  target_date: "2026-08-20",
  updated_at: "2026-08-02T00:00:00Z",
  updated_by: "user-1",
  version: 4,
  workspace_id: "workspace-1",
};

test("goal progress keeps exact backend percentage and only clamps its visual value", () => {
  assert.deepEqual(goalProgressPresentation("125.0000"), { exactText: "125%", visualValue: 100 });
  assert.deepEqual(goalProgressPresentation("-5.0000"), { exactText: "-5%", visualValue: 0 });
  assert.deepEqual(goalProgressPresentation("0.0000"), { exactText: "0%", visualValue: 0 });
  assert.deepEqual(goalProgressPresentation("33.3333"), { exactText: "33.3333%", visualValue: 33.3333 });
  assert.deepEqual(goalProgressPresentation("99999999999999999999.0000"), { exactText: "99999999999999999999%", visualValue: 100 });
  assert.deepEqual(goalProgressPresentation("1e1000"), { exactText: "1e1000%", visualValue: 100 });
  assert.deepEqual(goalProgressPresentation("60.2500"), { exactText: "60.25%", visualValue: 60.25 });
});

test("goal form defaults to workspace currency and sends no lifecycle status", () => {
  const form = initialGoalForm(" usd ");
  assert.equal(form.currency, "USD");
  Object.assign(form, {
    description: "  Резерв на поездку  ",
    name: "  Отпуск  ",
    targetAmount: "1000,50",
    targetDate: "2027-06-01",
  });
  assert.deepEqual(validateGoalForm(form), {});
  assert.deepEqual(goalCreateRequest(form), {
    currency: "USD",
    description: "Резерв на поездку",
    name: "Отпуск",
    target_amount: "1000.50",
    target_date: "2027-06-01",
  });
  const update = goalUpdateRequest(form, 7);
  assert.equal(update.version, 7);
  assert.equal("status" in update, false);
});

test("goal form rejects invalid currency, money and required name", () => {
  assert.deepEqual(validateGoalForm({
    currency: "RU",
    description: "",
    name: "",
    targetAmount: "-1",
    targetDate: "not-a-date",
  }), {
    currency: "Укажите трёхбуквенный код валюты, например RUB.",
    name: "Введите название цели.",
    targetAmount: "Целевая сумма должна быть положительной, с точностью до 4 знаков.",
    targetDate: "Укажите корректную дату.",
  });
});

test("Goal mutation requests preserve decimal strings and can explicitly clear nullable PATCH fields", () => {
  const form = {
    currency: "EUR",
    description: "",
    name: "Точная цель",
    targetAmount: "999999999999.9999",
    targetDate: "",
  };
  assert.deepEqual(goalUpdateRequest(form, 12), {
    currency: "EUR",
    description: null,
    name: "Точная цель",
    target_amount: "999999999999.9999",
    target_date: null,
    version: 12,
  });
  form.targetAmount = "0.0001";
  assert.equal(goalCreateRequest(form).target_amount, "0.0001");
});

test("each goal mutation identity is stable until reset", () => {
  const create = new GoalMutationIdentity();
  const contribution = new GoalMutationIdentity();
  const first = create.current();
  assert.equal(create.current(), first);
  assert.notEqual(contribution.current(), first);
  create.reset();
  assert.notEqual(create.current(), first);
});

test("deadline copy follows backend overdue and days_remaining projection", () => {
  assert.equal(goalDeadlineLabel(goal), "Просрочено на 2 дн.");
  assert.equal(goalDeadlineLabel({ ...goal, days_remaining: 0, overdue: false }), "Сегодня");
  assert.equal(goalDeadlineLabel({ ...goal, days_remaining: 3, overdue: false }), "3 дн. до срока");
  assert.equal(goalDeadlineLabel({ ...goal, days_remaining: null, target_date: null }), "Без срока");
});

test("date-only target formatting is pinned to UTC and cannot shift the calendar day", () => {
  assert.match(formatGoalTargetDate("2026-08-31"), /31/);
  assert.equal(formatGoalTargetDate("not-a-date"), "not-a-date");
});

test("goal errors map known codes and never expose structured backend values", () => {
  assert.match(goalErrorMessage(new ApiClientError("stale", "GOAL_VERSION_CONFLICT", 409)), /другой сессии/);
  assert.match(goalErrorMessage(new ApiClientError("invalid", "GOAL_CORRECTION_INVALID", 422)), /Исправление недопустимо/);
  assert.equal(
    goalErrorMessage(new ApiClientError("[object Object]", "UNKNOWN_GOAL_ERROR", 500)),
    "Backend отклонил команду цели.",
  );
  assert.equal(
    goalErrorMessage(new ApiClientError('{"token":"must-not-render"}', "UNKNOWN_GOAL_ERROR", 500)),
    "Backend отклонил команду цели.",
  );
  const bounded = goalErrorMessage(new ApiClientError("x".repeat(2000), "UNKNOWN_GOAL_ERROR", 500));
  assert.ok(bounded.length < 550);
});
