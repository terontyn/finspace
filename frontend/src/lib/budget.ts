import { ApiClientError } from "@/lib/api-client";
import type {
  BudgetAllocationInput,
  BudgetGroup,
  BudgetMoney,
  BudgetRolloverPolicy,
  BudgetUpsertRequest,
} from "@/types/budget";
import type { Category } from "@/types/finance";

export interface BudgetFormAllocation {
  categoryId: string;
  plannedAmount: string;
  note: string;
}

export interface BudgetFormState {
  currency: string;
  plannedIncome: string;
  rolloverPolicy: BudgetRolloverPolicy;
  allocations: BudgetFormAllocation[];
}

export interface BudgetFormErrors {
  allocations?: string;
  currency?: string;
  plannedIncome?: string;
}

const moneyInputPattern = /^\d+(?:[.,]\d{1,4})?$/;
const periodPattern = /^(\d{4})-(\d{2})$/;

export const rolloverPolicyLabels: Record<BudgetRolloverPolicy, string> = {
  none: "Не переносить",
  positive_only: "Только положительный остаток",
  full: "Полный остаток, включая перерасход",
};

export const budgetRevisionLabels = {
  copy: "Копирование",
  create: "Создание",
  delete: "Удаление плана",
  restore: "Восстановление",
  update: "Изменение",
} as const;

export class MutationIdentity {
  private value: string | null = null;

  current(): string {
    if (!this.value) this.value = createMutationKey();
    return this.value;
  }

  reset(): void {
    this.value = null;
  }
}

export function createMutationKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `budget-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function normalizeMoneyInput(value: string): string {
  return value.trim().replace(",", ".");
}

export function initialBudgetForm(currency: string): BudgetFormState {
  return { allocations: [], currency: currency.toUpperCase(), plannedIncome: "", rolloverPolicy: "none" };
}

export function budgetFormFromGroup(group: BudgetGroup): BudgetFormState {
  return {
    allocations: group.allocations.map((allocation) => ({
      categoryId: allocation.category_id,
      note: allocation.note ?? "",
      plannedAmount: allocation.planned,
    })),
    currency: group.currency,
    plannedIncome: group.planned_income,
    rolloverPolicy: group.rollover_policy,
  };
}

export function validateBudgetForm(form: BudgetFormState): BudgetFormErrors {
  const errors: BudgetFormErrors = {};
  if (!/^[A-Z]{3}$/.test(form.currency.trim().toUpperCase())) {
    errors.currency = "Укажите трёхбуквенный код валюты, например RUB.";
  }
  const income = normalizeMoneyInput(form.plannedIncome);
  if (!moneyInputPattern.test(income) || Number(income) < 0) {
    errors.plannedIncome = "Плановый доход должен быть неотрицательной суммой с точностью до 4 знаков.";
  }
  const categoryIds = new Set<string>();
  for (const allocation of form.allocations) {
    const amount = normalizeMoneyInput(allocation.plannedAmount);
    if (!allocation.categoryId || !moneyInputPattern.test(amount) || Number(amount) <= 0) {
      errors.allocations = "Для каждой категории укажите положительную плановую сумму.";
      break;
    }
    if (categoryIds.has(allocation.categoryId)) {
      errors.allocations = "Категория может присутствовать в плане только один раз.";
      break;
    }
    categoryIds.add(allocation.categoryId);
  }
  return errors;
}

export function budgetRequestFromForm(
  form: BudgetFormState,
  version: number | null,
): BudgetUpsertRequest {
  const allocations: BudgetAllocationInput[] = form.allocations.map((allocation) => ({
    category_id: allocation.categoryId,
    note: allocation.note.trim() || null,
    planned_amount: normalizeMoneyInput(allocation.plannedAmount),
  }));
  return {
    allocations,
    planned_income: normalizeMoneyInput(form.plannedIncome),
    rollover_policy: form.rolloverPolicy,
    ...(version === null ? {} : { version }),
  };
}

export function allowedBudgetCategories(categories: Category[]): Category[] {
  return categories.filter(
    (category) => !category.is_archived && ["expense", "both"].includes(category.category_type),
  );
}

export function shiftBudgetPeriod(period: string, delta: number): string {
  const match = periodPattern.exec(period);
  if (!match) throw new Error(`Invalid Budget period: ${period}`);
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1 + delta, 1));
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function currentBudgetPeriod(timezone: string, now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    month: "2-digit",
    timeZone: timezone,
    year: "numeric",
  }).formatToParts(now);
  const year = parts.find((part) => part.type === "year")?.value;
  const month = parts.find((part) => part.type === "month")?.value;
  if (!year || !month) throw new Error("Unable to determine the current Budget period");
  return `${year}-${month}`;
}

export function budgetPeriodLabel(period: string): string {
  const match = periodPattern.exec(period);
  if (!match) return period;
  return new Intl.DateTimeFormat("ru-RU", { month: "long", timeZone: "UTC", year: "numeric" })
    .format(new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1)));
}

export function formatBudgetMoney(value: BudgetMoney, currency: string): string {
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

export function isNegativeMoney(value: BudgetMoney): boolean {
  return value.startsWith("-") && !/^-0(?:\.0+)?$/.test(value);
}

export function isPositiveMoney(value: BudgetMoney): boolean {
  return !isNegativeMoney(value) && !/^0(?:\.0+)?$/.test(value);
}

const errorMessages: Record<string, string> = {
  BUDGET_ALLOCATION_INVALID: "Проверьте суммы и состав распределений.",
  BUDGET_CATEGORY_INVALID: "Одна из категорий недоступна для бюджета.",
  BUDGET_COPY_CATEGORY_CONFLICT: "Копирование невозможно: категории прошлого плана изменились или недоступны.",
  BUDGET_COPY_SOURCE_NOT_FOUND: "В прошлом месяце нет бюджета для копирования.",
  BUDGET_COPY_TARGET_EXISTS: "Бюджет этого месяца уже существует. Подтвердите его замену отдельно.",
  BUDGET_IDEMPOTENCY_CONFLICT: "Команда с таким идентификатором уже использована для других данных.",
  BUDGET_NOT_FOUND: "Бюджет не найден или уже недоступен.",
  BUDGET_PERIOD_FROZEN: "План зафиксирован закрытием месяца и доступен только для чтения.",
  BUDGET_RESTORE_CATEGORY_CONFLICT: "Восстановление невозможно: часть категорий плана больше недоступна.",
  BUDGET_RESTORE_REQUIRED: "Сначала восстановите удалённый план бюджета.",
  BUDGET_VERSION_CONFLICT: "Бюджет изменился в другой сессии. Загружена актуальная версия.",
};

function conflictCategoryIds(details: unknown): string[] {
  if (!details || typeof details !== "object") return [];
  const record = details as Record<string, unknown>;
  for (const key of ["category_ids", "conflicted_category_ids", "invalid_category_ids"]) {
    if (Array.isArray(record[key])) {
      return record[key]
        .filter((value): value is string => typeof value === "string")
        .map((value) => value.slice(0, 100))
        .slice(0, 10);
    }
  }
  return [];
}

export function budgetErrorMessage(error: unknown): string {
  if (!(error instanceof ApiClientError)) return "Не удалось выполнить команду бюджета.";
  const mapped = errorMessages[error.code] ?? "Backend отклонил команду бюджета.";
  const categoryIds = conflictCategoryIds(error.details);
  const categorySuffix = categoryIds.length
    ? ` Затронуто категорий: ${categoryIds.length}. ID: ${categoryIds.join(", ")}.`
    : "";
  const backendMessage = error.message.trim();
  const messageLooksStructured = backendMessage === "[object Object]" || backendMessage.startsWith("{") || backendMessage.startsWith("[");
  const backendSuffix = backendMessage && !messageLooksStructured && !mapped.includes(backendMessage)
    ? ` ${backendMessage.slice(0, 500)}`
    : "";
  return `${mapped}${categorySuffix}${backendSuffix}`;
}
