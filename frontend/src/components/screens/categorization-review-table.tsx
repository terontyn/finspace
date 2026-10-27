"use client";

import { APPLY_STATUS_LABELS, PREVIEW_STATUS_LABELS, applyOutcomeGroup } from "@/lib/categorization-review";
import { formatMoney } from "@/lib/money";

import type {
  CategorizationApplyItemResult,
  CategorizationPreviewItem,
} from "@/types/categorization";
import type { Account } from "@/types/finance";

interface CategorizationReviewTableProps {
  accounts: Account[];
  items: CategorizationPreviewItem[];
  loading: boolean;
  onToggle: (itemId: string) => void;
  pending: boolean;
  results: Map<string, CategorizationApplyItemResult>;
  selected: Set<string>;
}

function partyName(item: CategorizationPreviewItem): string {
  return item.transaction?.counterparty ?? item.transaction?.description ?? "Без описания";
}

/**
 * Rows render from persisted preview evidence (`rule_name` / `category_name` snapshots), so a
 * proposal stays readable even after the live rule or category changes.
 */
export function CategorizationReviewTable({
  accounts,
  items,
  loading,
  onToggle,
  pending,
  results,
  selected,
}: CategorizationReviewTableProps) {
  const accountNames = new Map(accounts.map((account) => [account.id, account.name]));
  return (
    <div className="review-table-wrap">
      <table className="review-table">
        <thead>
          <tr>
            <th scope="col">
              <span className="sr-only">Выбор</span>
            </th>
            <th scope="col">Дата</th>
            <th scope="col">Счёт</th>
            <th scope="col">Контрагент</th>
            <th scope="col">Сумма</th>
            <th scope="col">Статус</th>
            <th scope="col">Правило</th>
            <th scope="col">Категория</th>
            <th scope="col">Результат</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const eligible = item.status === "matched";
            const result = results.get(item.id);
            const snapshot = item.transaction;
            const label = `Выбрать операцию ${partyName(item)}`;
            return (
              <tr className={selected.has(item.id) ? "is-selected" : ""} key={item.id}>
                <td>
                  {eligible && !result ? (
                    <input
                      aria-label={label}
                      checked={selected.has(item.id)}
                      disabled={pending}
                      onChange={() => onToggle(item.id)}
                      type="checkbox"
                    />
                  ) : (
                    <span className="sr-only">Недоступно для применения</span>
                  )}
                </td>
                <td>
                  {snapshot ? (
                    <time dateTime={snapshot.occurred_at}>
                      {new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short" }).format(
                        new Date(snapshot.occurred_at),
                      )}
                    </time>
                  ) : (
                    "—"
                  )}
                </td>
                <td>{snapshot ? (accountNames.get(snapshot.account_id) ?? "—") : "—"}</td>
                <td>
                  <strong>{partyName(item)}</strong>
                  {snapshot?.description && snapshot.description !== snapshot.counterparty ? (
                    <small>{snapshot.description}</small>
                  ) : null}
                </td>
                <td className="amount-cell">
                  {snapshot ? formatMoney(snapshot.amount, snapshot.currency) : "—"}
                </td>
                <td>
                  <span className={`status-chip status-chip--${item.status}`}>
                    {PREVIEW_STATUS_LABELS[item.status]}
                  </span>
                </td>
                <td>{item.rule_name ?? "—"}</td>
                <td>{item.category_name ?? "—"}</td>
                <td>
                  {result ? (
                    <span
                      className={`result-chip result-chip--${applyOutcomeGroup(result.status)}`}
                      data-status={result.status}
                    >
                      {APPLY_STATUS_LABELS[result.status]}
                    </span>
                  ) : pending && selected.has(item.id) ? (
                    <span className="result-chip result-chip--pending">Применяем…</span>
                  ) : (
                    <span className="sr-only">Результата пока нет</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {loading ? (
        <p aria-live="polite" className="review-table__loading">
          Загружаем строки…
        </p>
      ) : null}
      {!loading && items.length === 0 ? <p>В этом списке нет строк.</p> : null}
    </div>
  );
}
