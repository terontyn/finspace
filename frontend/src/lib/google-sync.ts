import type { FullExportPreview, GoogleSheetStatus, SyncConflict } from "../types/google.ts";

export function googleStateLabel(status: GoogleSheetStatus): string {
  if (!status.configured) return "Провайдер не настроен";
  if (status.provider === "google_oauth") {
    if (status.connection.status === "revoked") return "Доступ Google отозван";
    if (!status.connection.connected) return "Google не подключён";
  }
  if (!status.binding_id) return "Binding не создан";
  if (!status.spreadsheet_registered && status.provider === "apps_script_bridge") {
    return "Ожидает регистрации таблицы";
  }
  if (status.status === "initializing") return "Начальный экспорт";
  if (status.status === "paused") return "Синхронизация приостановлена";
  if (status.status === "error") return "Ошибка синхронизации";
  if (status.provider === "apps_script_bridge" && !status.heartbeat_healthy) {
    return "Ожидает heartbeat";
  }
  if (status.sync_mode === "bidirectional") return "Двусторонняя синхронизация";
  if (status.sync_mode === "push_only") return "Только PostgreSQL → Sheets";
  return status.status ?? "Состояние неизвестно";
}

export function fullExportMessage(preview: FullExportPreview): string {
  return [
    preview.warning,
    `Операции: ${preview.transactions}`,
    `Счета: ${preview.accounts}`,
    `Категории: ${preview.categories}`,
    `Входящие DIRTY: ${preview.pending_changes}`,
    `Открытые конфликты: ${preview.open_conflicts}`,
    "PostgreSQL-записи удалены не будут.",
  ].join("\n");
}

export function conflictDiff(conflict: SyncConflict): {
  database: string;
  sheet: string;
} {
  return {
    database: JSON.stringify(conflict.database_payload, null, 2),
    sheet: JSON.stringify(conflict.sheet_payload, null, 2),
  };
}

const fieldLabels: Record<string, string> = {
  account_id: "Счёт",
  amount: "Сумма",
  category_id: "Категория",
  color: "Цвет",
  comment: "Комментарий",
  counterparty: "Контрагент",
  currency: "Валюта",
  description: "Описание",
  icon: "Иконка",
  institution: "Организация",
  is_archived: "Архив",
  name: "Название",
  occurred_at: "Дата и время",
  opening_balance: "Начальный остаток",
  parent_id: "Родительская категория",
  row_hash: "Контрольная сумма строки",
  sort_order: "Порядок",
  status: "Статус",
  target_account_id: "Целевой счёт",
  transaction_type: "Тип операции",
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && !Array.isArray(value) && typeof value === "object"
    ? value as Record<string, unknown>
    : {};
}

export interface ConflictFieldDiff {
  field: string;
  label: string;
  database: unknown;
  external: unknown;
}

export function conflictFieldDiffs(conflict: SyncConflict): ConflictFieldDiff[] {
  const changed = record(conflict.sheet_payload.changed_fields);
  const visible = record(conflict.sheet_payload.visible_row);
  return conflict.conflicting_fields.map((field) => ({
    field,
    label: fieldLabels[field] ?? field,
    database: conflict.database_payload[field],
    external: field in changed
      ? changed[field]
      : field in visible
        ? visible[field]
        : conflict.sheet_payload[field],
  }));
}

export function conflictValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function conflictEntityLabel(entityType: string): string {
  return ({ account: "Счёт", category: "Категория", transaction: "Операция" })[entityType]
    ?? entityType;
}

export function conflictResolutionMessage(resolution: string): string {
  const labels: Record<string, string> = {
    keep_database: "оставить PostgreSQL",
    keep_sheet: "оставить Google Sheets",
    manual_merge: "применить ручное объединение",
  };
  return `Подтвердите решение: ${labels[resolution] ?? resolution}.`;
}

export function parseMergedPayload(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(
      "Ручное объединение должно быть JSON-объектом с изменяемыми полями.",
    );
  }
  return parsed as Record<string, unknown>;
}
