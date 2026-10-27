"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { ActionDialog } from "@/components/ui/action-dialog";
import { EntityDrawer } from "@/components/ui/entity-drawer";
import {
  APPLY_STATUS_LABELS,
  fetchCategorizationApplyHistory,
  fetchCategorizationApplyHistoryDetail,
  reReviewTransactionIds,
} from "@/lib/categorization-review";
import type {
  CategorizationApplyHistoryDetail,
  CategorizationApplyHistoryOperation,
} from "@/types/categorization";

const HISTORY_PAGE_SIZE = 20;

interface CategorizationApplyHistoryProps {
  onError: (error: unknown) => void;
  onReReview: (transactionIds: string[]) => Promise<void>;
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "Время недоступно";
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function needsReviewCount(operation: CategorizationApplyHistoryOperation): number {
  return (
    operation.counts.transaction_changed +
    operation.counts.rule_changed +
    operation.counts.category_changed +
    operation.counts.no_match +
    operation.counts.failed
  );
}

export function CategorizationApplyHistory({
  onError,
  onReReview,
}: CategorizationApplyHistoryProps) {
  const [operations, setOperations] = useState<CategorizationApplyHistoryOperation[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<CategorizationApplyHistoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [creating, setCreating] = useState(false);

  const load = useCallback(
    async (nextOffset: number) => {
      setLoading(true);
      try {
        const page = await fetchCategorizationApplyHistory({
          limit: HISTORY_PAGE_SIZE,
          offset: nextOffset,
        });
        setOperations(page.items);
        setTotal(page.page.total);
        setOffset(nextOffset);
      } catch (error) {
        onError(error);
      } finally {
        setLoading(false);
      }
    },
    [onError],
  );

  useEffect(() => {
    let active = true;
    void fetchCategorizationApplyHistory({ limit: HISTORY_PAGE_SIZE, offset: 0 })
      .then((page) => {
        if (!active) return;
        setOperations(page.items);
        setTotal(page.page.total);
      })
      .catch((error: unknown) => {
        if (active) onError(error);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [onError]);

  const openDetail = useCallback(
    async (operationId: string) => {
      setDetailLoading(true);
      try {
        setDetail(
          await fetchCategorizationApplyHistoryDetail(operationId, { limit: 100, offset: 0 }),
        );
      } catch (error) {
        onError(error);
      } finally {
        setDetailLoading(false);
      }
    },
    [onError],
  );

  const eligibleIds = useMemo(
    () => (detail ? reReviewTransactionIds(detail.results) : []),
    [detail],
  );

  const confirmReReview = useCallback(async () => {
    if (eligibleIds.length === 0 || creating) return;
    setCreating(true);
    try {
      await onReReview(eligibleIds);
      setConfirming(false);
      setDetail(null);
    } catch (error) {
      onError(error);
    } finally {
      setCreating(false);
    }
  }, [creating, eligibleIds, onError, onReReview]);

  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + operations.length, total);

  return (
    <section aria-labelledby="apply-history-title" className="categorization-history panel-card">
      <header className="categorization-history__heading">
        <div>
          <span className="kicker">Операционный журнал</span>
          <h3 id="apply-history-title">История применений</h3>
          <p>
            Исторические попытки применения. Записи отражают сохранённый результат на момент
            выполнения, а не текущие рекомендации.
          </p>
        </div>
        <button className="text-button" disabled={loading} onClick={() => void load(offset)} type="button">
          Обновить историю
        </button>
      </header>

      {loading ? <p className="categorization-history__empty">Загружаем историю…</p> : null}
      {!loading && operations.length === 0 ? (
        <p className="categorization-history__empty">Попыток применения пока нет.</p>
      ) : null}

      <div className="categorization-history__list">
        {operations.map((operation) => (
          <article key={operation.id}>
            <div>
              <strong>Историческая попытка применения</strong>
              <span>{formatTimestamp(operation.created_at)}</span>
              <small>{operation.actor.display_name ?? "Пользователь недоступен"}</small>
            </div>
            <div className="categorization-history__counts">
              <span>Запрошено {operation.requested_count}</span>
              <span>Результатов {operation.result_count}</span>
              <span>Применено {operation.counts.applied}</span>
              <span>Повторно проверить {needsReviewCount(operation)}</span>
            </div>
            <div>
              <span className="status-chip">
                {operation.status === "completed" ? "Завершена" : "Выполняется"}
              </span>
              <button
                className="text-button"
                disabled={detailLoading}
                onClick={() => void openDetail(operation.id)}
                type="button"
              >
                Открыть результаты
              </button>
            </div>
          </article>
        ))}
      </div>

      {total > HISTORY_PAGE_SIZE ? (
        <div className="review-pagination">
          <button
            className="text-button"
            disabled={offset === 0 || loading}
            onClick={() => void load(Math.max(0, offset - HISTORY_PAGE_SIZE))}
            type="button"
          >
            Назад
          </button>
          <span>{pageStart}–{pageEnd} из {total}</span>
          <button
            className="text-button"
            disabled={offset + HISTORY_PAGE_SIZE >= total || loading}
            onClick={() => void load(offset + HISTORY_PAGE_SIZE)}
            type="button"
          >
            Вперёд
          </button>
        </div>
      ) : null}

      {detail ? (
        <EntityDrawer
          ariaLabel="Исторические результаты категоризации"
          eyebrow="Историческая попытка применения"
          onClose={() => setDetail(null)}
          subtitle={`Операция ${detail.id}`}
          title={detail.status === "completed" ? "Завершённые результаты" : "Частичные результаты"}
        >
          <div className="categorization-history__detail-summary">
            <span>Запрошено {detail.requested_count}</span>
            <span>Сохранено результатов {detail.result_count}</span>
            {detail.status === "in_progress" ? (
              <p>Операция ещё не завершена. История показывает только уже зафиксированные факты.</p>
            ) : null}
          </div>
          <div className="categorization-history__results">
            {detail.results.map((result) => (
              <article key={`${detail.id}-${result.sequence}`}>
                <div>
                  <strong>#{result.sequence + 1} · {APPLY_STATUS_LABELS[result.status]}</strong>
                  <code>{result.status}</code>
                </div>
                <code>{result.transaction_id ?? "transaction_id отсутствует"}</code>
                {result.error_code ? <span>Ошибка: {result.error_code}</span> : null}
                {result.expected_version !== null || result.current_version !== null ? (
                  <span>
                    Версия: ожидалась {result.expected_version ?? "—"}, сохранена {result.current_version ?? "—"}
                  </span>
                ) : null}
              </article>
            ))}
          </div>
          {eligibleIds.length > 0 ? (
            <button className="primary-button" onClick={() => setConfirming(true)} type="button">
              Проверить изменившиеся снова ({eligibleIds.length})
            </button>
          ) : (
            <p className="categorization-history__empty">Нет результатов, доступных для повторной проверки.</p>
          )}
        </EntityDrawer>
      ) : null}

      {confirming ? (
        <ActionDialog
          description="Будет создан новый список по текущим правилам и текущему состоянию операций. Ничего не применится автоматически."
          eyebrow="Безопасная повторная проверка"
          onClose={() => setConfirming(false)}
          title="Создать новый список?"
        >
          <div className="dialog-actions">
            <button className="primary-button" disabled={creating} onClick={() => void confirmReReview()} type="button">
              {creating ? "Создаём…" : "Создать новый список"}
            </button>
            <button className="secondary-button" disabled={creating} onClick={() => setConfirming(false)} type="button">
              Отмена
            </button>
          </div>
        </ActionDialog>
      ) : null}
    </section>
  );
}
