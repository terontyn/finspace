"use client";

import type { CategorizationPreviewHeader } from "@/types/categorization";

interface CategorizationPreviewSummaryProps {
  header: CategorizationPreviewHeader;
}

const SECONDARY: { key: keyof CategorizationPreviewHeader["summary"]; label: string }[] = [
  { key: "no_match", label: "Нет правила" },
  { key: "already_categorized", label: "Уже категоризованы" },
  { key: "transfer", label: "Переводы" },
  { key: "split", label: "Разделённые" },
  { key: "reconciled", label: "Сверенные" },
  { key: "closed_period", label: "Закрытый период" },
  { key: "not_found", label: "Не найдены" },
];

/**
 * The review metric is "X proposed / Y selected". Zero buckets are dropped rather than rendered as
 * a wall of noughts.
 */
export function CategorizationPreviewSummary({ header }: CategorizationPreviewSummaryProps) {
  const { summary } = header;
  const secondary = SECONDARY.filter((entry) => summary[entry.key] > 0);
  return (
    <section aria-label="Итоги списка" className="preview-summary">
      <p className="preview-summary__headline">
        <strong>{summary.matched}</strong> предложено из <strong>{summary.selected}</strong> отобранных
      </p>
      {secondary.length ? (
        <ul className="preview-summary__rest">
          {secondary.map((entry) => (
            <li key={entry.key}>
              <span>{entry.label}</span>
              <b>{summary[entry.key]}</b>
            </li>
          ))}
        </ul>
      ) : null}
      <p className="preview-summary__meta">
        Список составлен{" "}
        <time dateTime={header.created_at}>
          {new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(
            new Date(header.created_at),
          )}
        </time>{" "}
        и действует до{" "}
        <time dateTime={header.expires_at}>
          {new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(
            new Date(header.expires_at),
          )}
        </time>
        . Ничего ещё не изменено.
      </p>
    </section>
  );
}
