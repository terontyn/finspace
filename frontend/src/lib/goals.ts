import { ApiClientError } from "@/lib/api-client";
import type { Goal, GoalCreateRequest, GoalStatus, GoalUpdateRequest } from "@/types/goals";

export interface GoalFormState {
  name: string;
  description: string;
  currency: string;
  targetAmount: string;
  targetDate: string;
}

export interface GoalFormErrors {
  name?: string;
  currency?: string;
  targetAmount?: string;
  targetDate?: string;
}

const positiveMoneyPattern = /^\d+(?:[.,]\d{1,4})?$/;

export const goalStatusLabels: Record<GoalStatus, string> = {
  active: "Активна",
  paused: "Приостановлена",
  completed: "Завершена",
  cancelled: "Отменена",
};

export class GoalMutationIdentity {
  private value: string | null = null;

  current(): string {
    if (!this.value) this.value = createGoalMutationKey();
    return this.value;
  }

  reset(): void {
    this.value = null;
  }
}

export function createGoalMutationKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `goal-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function initialGoalForm(currency: string): GoalFormState {
  return {
    currency: currency.trim().toUpperCase(),
    description: "",
    name: "",
    targetAmount: "",
    targetDate: "",
  };
}

export function goalFormFromRecord(goal: Goal): GoalFormState {
  return {
    currency: goal.currency,
    description: goal.description ?? "",
    name: goal.name,
    targetAmount: goal.target_amount,
    targetDate: goal.target_date ?? "",
  };
}

export function normalizeGoalMoney(value: string): string {
  return value.trim().replace(",", ".");
}

export function validateGoalForm(form: GoalFormState): GoalFormErrors {
  const errors: GoalFormErrors = {};
  if (!form.name.trim()) errors.name = "Введите название цели.";
  if (!/^[A-Z]{3}$/.test(form.currency.trim().toUpperCase())) {
    errors.currency = "Укажите трёхбуквенный код валюты, например RUB.";
  }
  const amount = normalizeGoalMoney(form.targetAmount);
  if (!positiveMoneyPattern.test(amount) || Number(amount) <= 0) {
    errors.targetAmount = "Целевая сумма должна быть положительной, с точностью до 4 знаков.";
  }
  if (form.targetDate && !/^\d{4}-\d{2}-\d{2}$/.test(form.targetDate)) {
    errors.targetDate = "Укажите корректную дату.";
  }
  return errors;
}

function commonRequest(form: GoalFormState): GoalCreateRequest {
  return {
    currency: form.currency.trim().toUpperCase(),
    description: form.description.trim() || null,
    name: form.name.trim(),
    target_amount: normalizeGoalMoney(form.targetAmount),
    target_date: form.targetDate || null,
  };
}

export function goalCreateRequest(form: GoalFormState): GoalCreateRequest {
  return commonRequest(form);
}

export function goalUpdateRequest(form: GoalFormState, version: number): GoalUpdateRequest {
  return { ...commonRequest(form), version };
}

export function formatGoalMoney(value: string, currency: string): string {
  if (!/^-?\d+(?:\.\d{1,4})?$/.test(value)) return `${value} ${currency}`;
  try {
    return new Intl.NumberFormat("ru-RU", {
      currency,
      maximumFractionDigits: 4,
      minimumFractionDigits: 2,
      style: "currency",
    }).format(Number(value));
  } catch {
    return `${value} ${currency}`;
  }
}

export function goalProgressPresentation(value: string): {
  exactText: string;
  visualValue: number;
} {
  const numeric = Number(value);
  const visualValue = Number.isNaN(numeric) || numeric <= 0
    ? 0
    : numeric >= 100
      ? 100
      : numeric;
  const trimmed = /^-?\d+(?:\.\d+)?$/.test(value)
    ? value.replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1")
    : value;
  return { exactText: `${trimmed}%`, visualValue };
}

export function goalDeadlineLabel(goal: Goal): string {
  if (goal.target_date === null || goal.days_remaining === null) return "Без срока";
  if (goal.overdue) return `Просрочено на ${Math.abs(goal.days_remaining)} дн.`;
  if (goal.days_remaining === 0) return "Сегодня";
  return `${goal.days_remaining} дн. до срока`;
}

export function formatGoalTargetDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  const instant = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: "UTC" }).format(instant);
}

const goalErrorMessages: Record<string, string> = {
  GOAL_CONTRIBUTION_INVALID: "Проверьте сумму или дату вклада.",
  GOAL_CONTRIBUTION_NOT_ALLOWED: "Вклад можно добавить только в активную цель.",
  GOAL_CORRECTION_INVALID: "Исправление недопустимо. Исходное событие остаётся неизменным.",
  GOAL_CURRENCY_IMMUTABLE: "Валюту нельзя изменить после первого события вклада.",
  GOAL_IDEMPOTENCY_CONFLICT: "Эта команда уже использована для других данных.",
  GOAL_NOT_FOUND: "Цель не найдена или больше недоступна.",
  GOAL_RESTORE_REQUIRED: "Сначала восстановите удалённую цель.",
  GOAL_STATUS_INVALID: "Команда недоступна в текущем состоянии цели.",
  GOAL_TARGET_NOT_REACHED: "Завершить цель можно только после достижения целевой суммы.",
  GOAL_VERSION_CONFLICT: "Цель изменилась в другой сессии. Загружена актуальная версия.",
};

export function goalErrorMessage(error: unknown): string {
  if (!(error instanceof ApiClientError)) return "Не удалось выполнить команду цели.";
  const mapped = goalErrorMessages[error.code] ?? "Backend отклонил команду цели.";
  const backendMessage = error.message.trim();
  const structured = backendMessage === "[object Object]" || backendMessage.startsWith("{") || backendMessage.startsWith("[");
  const suffix = backendMessage && !structured && !mapped.includes(backendMessage)
    ? ` ${backendMessage.slice(0, 500)}`
    : "";
  return `${mapped}${suffix}`;
}

export function isUncertainGoalError(error: unknown): boolean {
  return error instanceof ApiClientError && error.status === 0;
}
