import { ApiClientError } from "./api-client";

import type { CategorizationRule, CategorizationTransactionType } from "../types/categorization";

export interface CategorizationRuleForm {
  name: string;
  priority: string;
  isActive: boolean;
  transactionType: CategorizationTransactionType | "";
  accountId: string;
  payeeId: string;
  counterpartyContains: string;
  descriptionContains: string;
  categoryId: string;
}

export interface CategorizationUiError {
  code: string;
  message: string;
  requestId?: string;
}

export function initialCategorizationRuleForm(): CategorizationRuleForm {
  return {
    accountId: "",
    categoryId: "",
    counterpartyContains: "",
    descriptionContains: "",
    isActive: true,
    name: "",
    payeeId: "",
    priority: "100",
    transactionType: "",
  };
}

export function categorizationRuleFormFromRecord(rule: CategorizationRule): CategorizationRuleForm {
  return {
    accountId: rule.account_id ?? "",
    categoryId: rule.category_id,
    counterpartyContains: rule.counterparty_contains ?? "",
    descriptionContains: rule.description_contains ?? "",
    isActive: rule.is_active,
    name: rule.name,
    payeeId: rule.payee_id ?? "",
    priority: String(rule.priority),
    transactionType: rule.transaction_type ?? "",
  };
}

export function categorizationFormHasMatcher(form: CategorizationRuleForm): boolean {
  return Boolean(
    form.transactionType ||
      form.accountId ||
      form.payeeId ||
      form.counterpartyContains.trim() ||
      form.descriptionContains.trim(),
  );
}

export function categorizationRuleMutation(form: CategorizationRuleForm, editing: CategorizationRule | null) {
  const body = {
    account_id: form.accountId || null,
    category_id: form.categoryId,
    counterparty_contains: form.counterpartyContains.trim() || null,
    description_contains: form.descriptionContains.trim() || null,
    is_active: form.isActive,
    name: form.name.trim(),
    payee_id: form.payeeId || null,
    priority: Number(form.priority),
    transaction_type: form.transactionType || null,
  };
  return editing
    ? {
        body: { ...body, version: editing.version },
        method: "PATCH" as const,
        path: `/api/v1/categorization-rules/${editing.id}`,
      }
    : { body, method: "POST" as const, path: "/api/v1/categorization-rules" };
}

const errorMessages: Record<string, string> = {
  ACCOUNT_NOT_FOUND: "Выбранный счёт больше недоступен. Обновите данные и выберите другой счёт.",
  CATEGORY_NOT_FOUND: "Выбранная категория больше недоступна. Обновите данные и выберите другую категорию.",
  CATEGORIZATION_MATCHER_REQUIRED: "Добавьте хотя бы одно условие правила.",
  CATEGORIZATION_RULE_CHANGED: "Правило изменилось во время операции. Выполните предпросмотр ещё раз.",
  CATEGORIZATION_RULE_NOT_FOUND: "Правило не найдено или уже недоступно.",
  INVALID_CATEGORY_TYPE: "Тип целевой категории не подходит выбранному типу операции.",
  MONTH_CLOSED: "Период закрыт. Категоризацию этой операции изменить нельзя.",
  PAYEE_NOT_FOUND: "Выбранный получатель больше недоступен. Обновите данные и выберите другого получателя.",
  RECONCILED_TRANSACTION_IMMUTABLE: "Сверенная операция неизменяема.",
  VERSION_CONFLICT: "Данные изменились. Обновите список и повторите действие с актуальной версией.",
};

export function categorizationUiError(error: unknown): CategorizationUiError {
  if (error instanceof ApiClientError) {
    return {
      code: error.code,
      message:
        errorMessages[error.code] ??
        (error.status === 403
          ? "Недостаточно прав для этого действия."
          : error.status === 404
            ? "Запрошенные данные не найдены."
            : error.status === 422
              ? "Проверьте заполненные поля. Backend отклонил данные."
              : error.message),
      requestId: error.requestId,
    };
  }
  return { code: "UNEXPECTED_ERROR", message: "Не удалось выполнить действие." };
}
