"use client";

import type { Account, Payee, TransactionSource, TransactionStatus, TransactionType } from "@/types/finance";

export interface ReviewFilters {
  occurredFrom: string;
  occurredTo: string;
  accountId: string;
  payeeId: string;
  transactionType: TransactionType | "";
  status: TransactionStatus | "";
  source: TransactionSource | "";
}

export function initialReviewFilters(): ReviewFilters {
  return {
    accountId: "",
    occurredFrom: "",
    occurredTo: "",
    payeeId: "",
    source: "",
    status: "",
    transactionType: "",
  };
}

interface CategorizationReviewFiltersProps {
  accounts: Account[];
  busy: boolean;
  filters: ReviewFilters;
  onChange: (filters: ReviewFilters) => void;
  onSubmit: () => void;
  payees: Payee[];
}

const TYPES: { label: string; value: TransactionType }[] = [
  { label: "Расход", value: "expense" },
  { label: "Доход", value: "income" },
  { label: "Перевод", value: "transfer" },
  { label: "Возврат", value: "refund" },
  { label: "Корректировка", value: "adjustment" },
];

const STATUSES: { label: string; value: TransactionStatus }[] = [
  { label: "Черновик", value: "draft" },
  { label: "Подтверждена", value: "confirmed" },
  { label: "Сверена", value: "reconciled" },
  { label: "Отменена", value: "cancelled" },
];

const SOURCES: { label: string; value: TransactionSource }[] = [
  { label: "Вручную", value: "manual" },
  { label: "API", value: "api" },
  { label: "Импорт", value: "import" },
  { label: "Система", value: "system" },
  { label: "Google Sheets", value: "google_sheets" },
  { label: "Автоматизация", value: "automation" },
  { label: "Telegram", value: "telegram" },
];

/** Only backend-supported filters. The backend itself restricts selection to categoryless rows. */
export function CategorizationReviewFilters({
  accounts,
  busy,
  filters,
  onChange,
  onSubmit,
  payees,
}: CategorizationReviewFiltersProps) {
  return (
    <form
      className="review-filters"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="review-filters__grid">
        <label>
          <span>Дата с</span>
          <input
            onChange={(event) => onChange({ ...filters, occurredFrom: event.target.value })}
            type="date"
            value={filters.occurredFrom}
          />
        </label>
        <label>
          <span>Дата по</span>
          <input
            onChange={(event) => onChange({ ...filters, occurredTo: event.target.value })}
            type="date"
            value={filters.occurredTo}
          />
        </label>
        <label>
          <span>Счёт</span>
          <select
            onChange={(event) => onChange({ ...filters, accountId: event.target.value })}
            value={filters.accountId}
          >
            <option value="">Любой</option>
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Получатель</span>
          <select
            onChange={(event) => onChange({ ...filters, payeeId: event.target.value })}
            value={filters.payeeId}
          >
            <option value="">Любой</option>
            {payees.map((payee) => (
              <option key={payee.id} value={payee.id}>
                {payee.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Тип</span>
          <select
            onChange={(event) =>
              onChange({ ...filters, transactionType: event.target.value as TransactionType | "" })
            }
            value={filters.transactionType}
          >
            <option value="">Любой</option>
            {TYPES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Статус</span>
          <select
            onChange={(event) =>
              onChange({ ...filters, status: event.target.value as TransactionStatus | "" })
            }
            value={filters.status}
          >
            <option value="">Любой</option>
            {STATUSES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Источник</span>
          <select
            onChange={(event) =>
              onChange({ ...filters, source: event.target.value as TransactionSource | "" })
            }
            value={filters.source}
          >
            <option value="">Любой</option>
            {SOURCES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="review-filters__actions">
        <button className="primary-button" disabled={busy} type="submit">
          {busy ? "Составляем список…" : "Составить список"}
        </button>
        <p className="review-filters__hint">
          Отбираются только операции без категории. Список ничего не меняет — это предложения для
          проверки.
        </p>
      </div>
    </form>
  );
}
