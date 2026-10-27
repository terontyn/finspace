import type { ImportBatch, ImportRow } from "@/types/finance";

export const importMappingTargets = [
  ["date", "Дата"],
  ["time", "Время"],
  ["transaction_type", "Тип операции"],
  ["amount", "Сумма"],
  ["income_amount", "Сумма дохода"],
  ["expense_amount", "Сумма расхода"],
  ["currency", "Валюта"],
  ["account", "Счёт"],
  ["target_account", "Целевой счёт"],
  ["category", "Категория"],
  ["counterparty", "Контрагент"],
  ["description", "Описание"],
  ["comment", "Комментарий"],
  ["status", "Статус"],
  ["external_id", "Внешний ID"],
] as const;

export function mappingMissing(mapping: Record<string, string>): string[] {
  const missing: string[] = [];
  if (!mapping.date) missing.push("дата");
  if (!mapping.account) missing.push("счёт");
  const hasSingleAmount = Boolean(mapping.amount);
  const hasSplitAmount = Boolean(mapping.income_amount || mapping.expense_amount);
  if (!hasSingleAmount && !hasSplitAmount) missing.push("сумма");
  if (hasSingleAmount && !mapping.transaction_type) missing.push("тип операции");
  return missing;
}

export function importStep(batch: ImportBatch | null): 1 | 2 | 3 | 4 {
  if (!batch) return 1;
  if (batch.status === "mapping_required" || batch.status === "uploaded") return 2;
  if (["parsed", "validated", "ready"].includes(batch.status)) return 3;
  return 4;
}

const statusLabels: Record<string, string> = {
  cancelled: "Отменён",
  duplicate: "Дубликат",
  imported: "Импортирован",
  invalid: "Ошибка",
  mapping_required: "Нужно сопоставление",
  parsed: "Сопоставлен",
  raw: "Исходная строка",
  ready: "Готов",
  rolled_back: "Отменён откатом",
  skipped: "Пропущен",
  uploaded: "Загружен",
  valid: "Готова",
  validated: "Проверен",
};

export function importStatusLabel(status: string): string {
  return statusLabels[status] ?? status;
}

export function rowSource(row: ImportRow): string {
  return Object.entries(row.raw_data)
    .filter(([, value]) => value !== null && String(value).trim() !== "")
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(" · ") || "Пустая строка";
}

const importErrorMessages: Record<string, string> = {
  "Amount is not a decimal number": "Сумма указана не в числовом формате",
  "Amount must be positive": "Сумма должна быть больше нуля",
  "Category type does not match transaction type": "Тип категории не соответствует типу операции",
  "Currency does not match account currency": "Валюта строки не совпадает с валютой счёта",
  "Date format is not supported": "Формат даты не поддерживается",
  "Income and expense columns cannot both contain a value": "В строке одновременно указаны доход и расход",
  "Status is not supported": "Статус операции не поддерживается",
  "Time format is not supported": "Формат времени не поддерживается",
  "Transaction type is not supported for import": "Тип операции не поддерживается",
  "Transfer accounts must be distinct and use the same currency": "Счета перевода должны различаться и иметь одну валюту",
  "Transfer cannot contain a category": "Для перевода нельзя указывать категорию",
  "Workspace timezone is invalid": "Часовой пояс пространства настроен неверно",
};

function importErrorMessage(message: string): string {
  if (importErrorMessages[message]) return importErrorMessages[message];
  const namedEntity = /^(Unknown|Ambiguous) (account|category): (.+)$/.exec(message);
  if (!namedEntity) return "Строка не прошла проверку";
  const [, reason, entity, value] = namedEntity;
  if (entity === "account") {
    return reason === "Unknown"
      ? `Счёт не найден: ${value}`
      : `Счёт указан неоднозначно: ${value}`;
  }
  return reason === "Unknown"
    ? `Категория не найдена: ${value}`
    : `Категория указана неоднозначно: ${value}`;
}

export function rowErrors(row: ImportRow): string {
  return row.validation_errors?.map((item) => importErrorMessage(item.message ?? "")).join("; ")
    ?? (row.status === "duplicate" ? "Совпадает с существующей или другой строкой файла" : "—");
}
