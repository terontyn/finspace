"use client";

import { APPLY_STATUS_LABELS, applyOutcomeGroup, requiresNewPreview } from "@/lib/categorization-review";

import type { CategorizationApplyResponse } from "@/types/categorization";

interface CategorizationApplyResultsProps {
  onNewPreview: () => void;
  onReviewRemaining: () => void;
  response: CategorizationApplyResponse;
}

const GROUP_LABELS = {
  applied: "Применено",
  failed: "Не удалось",
  needs_preview: "Нужен новый список",
  not_applicable: "Не применимо",
} as const;

const GROUP_ORDER = ["applied", "needs_preview", "not_applicable", "failed"] as const;

export function CategorizationApplyResults({
  onNewPreview,
  onReviewRemaining,
  response,
}: CategorizationApplyResultsProps) {
  const { summary } = response;
  const statuses = response.results.map((result) => result.status);
  const needsPreview = requiresNewPreview(statuses);
  const grouped = new Map<string, { count: number; label: string; status: string }[]>();
  for (const result of response.results) {
    const group = applyOutcomeGroup(result.status);
    const bucket = grouped.get(group) ?? [];
    const existing = bucket.find((entry) => entry.status === result.status);
    if (existing) existing.count += 1;
    else bucket.push({ count: 1, label: APPLY_STATUS_LABELS[result.status], status: result.status });
    grouped.set(group, bucket);
  }

  return (
    <section aria-label="Результаты применения" className="apply-results">
      <h3>Результаты</h3>
      <ul className="apply-results__totals">
        <li>
          <span>Запрошено</span>
          <b>{summary.requested}</b>
        </li>
        <li>
          <span>Применено</span>
          <b>{summary.applied}</b>
        </li>
        <li>
          <span>Расхождения</span>
          <b>{summary.conflicts}</b>
        </li>
        <li>
          <span>Не применено</span>
          <b>{summary.not_applied}</b>
        </li>
        <li>
          <span>Ошибки</span>
          <b>{summary.failed}</b>
        </li>
      </ul>
      {GROUP_ORDER.map((group) => {
        const bucket = grouped.get(group);
        if (!bucket || bucket.length === 0) return null;
        return (
          <div className="apply-results__group" key={group}>
            <h4>{GROUP_LABELS[group]}</h4>
            <ul>
              {bucket.map((entry) => (
                <li key={entry.status}>
                  <span className={`result-chip result-chip--${group}`} data-status={entry.status}>
                    {entry.label}
                  </span>
                  <b>{entry.count}</b>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
      {needsPreview ? (
        <p className="notice notice--info" role="status">
          Часть операций или правил изменилась после составления списка. Составьте новый список,
          чтобы увидеть актуальные предложения.
        </p>
      ) : null}
      <div className="apply-results__actions">
        <button className="primary-button" onClick={onNewPreview} type="button">
          Составить новый список
        </button>
        <button className="text-button" onClick={onReviewRemaining} type="button">
          Вернуться к оставшимся предложениям
        </button>
      </div>
    </section>
  );
}
