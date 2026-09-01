"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { CategorizationApplyResults } from "@/components/screens/categorization-apply-results";
import { CategorizationPreviewSummary } from "@/components/screens/categorization-preview-summary";
import {
  CategorizationReviewFilters,
  initialReviewFilters,
  type ReviewFilters,
} from "@/components/screens/categorization-review-filters";
import { CategorizationReviewTable } from "@/components/screens/categorization-review-table";
import { ApiClientError, apiClient, type WorkspaceRole } from "@/lib/api-client";
import {
  MAX_APPLY_ITEMS,
  REVIEW_PAGE_SIZE,
  applyCategorizationPreview,
  createCategorizationPreview,
  fetchCategorizationPreviewItems,
  nextApplyAttempt,
  retainAttemptForSelection,
  type ApplyAttempt,
} from "@/lib/categorization-review";

import type {
  CategorizationApplyItemResult,
  CategorizationApplyResponse,
  CategorizationPreviewFilterSelection,
  CategorizationPreviewHeader,
  CategorizationPreviewItem,
} from "@/types/categorization";
import type { Account, Paged, Payee } from "@/types/finance";

interface CategorizationReviewScreenProps {
  onError: (error: unknown) => void;
  role: WorkspaceRole | null;
  roleLoading: boolean;
}

interface UiError {
  code: string;
  message: string;
  requestId?: string;
}

type Stage = "select" | "review" | "results";

function toUiError(error: unknown): UiError {
  if (error instanceof ApiClientError) {
    return { code: error.code, message: error.message, requestId: error.requestId };
  }
  return { code: "UNEXPECTED", message: "Не удалось выполнить запрос." };
}

/** No authoritative HTTP response came back, so the outcome is genuinely unknown. */
function isAmbiguous(error: unknown): boolean {
  return error instanceof ApiClientError && error.status === 0;
}

function buildSelection(filters: ReviewFilters): CategorizationPreviewFilterSelection {
  const selection: CategorizationPreviewFilterSelection = { mode: "filter" };
  if (filters.occurredFrom) selection.occurred_from = `${filters.occurredFrom}T00:00:00Z`;
  if (filters.occurredTo) selection.occurred_to = `${filters.occurredTo}T23:59:59Z`;
  if (filters.accountId) selection.account_id = filters.accountId;
  if (filters.payeeId) selection.payee_id = filters.payeeId;
  if (filters.transactionType) selection.transaction_type = filters.transactionType;
  if (filters.status) selection.status = filters.status;
  if (filters.source) selection.source = filters.source;
  return selection;
}

export function CategorizationReviewScreen({
  onError,
  role,
  roleLoading,
}: CategorizationReviewScreenProps) {
  const [filters, setFilters] = useState<ReviewFilters>(initialReviewFilters);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [payees, setPayees] = useState<Payee[]>([]);
  const [header, setHeader] = useState<CategorizationPreviewHeader | null>(null);
  const [items, setItems] = useState<CategorizationPreviewItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [stage, setStage] = useState<Stage>("select");
  const [creating, setCreating] = useState(false);
  const [loadingItems, setLoadingItems] = useState(false);
  const [applying, setApplying] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [attempt, setAttempt] = useState<ApplyAttempt | null>(null);
  const [applyResponse, setApplyResponse] = useState<CategorizationApplyResponse | null>(null);
  const [error, setError] = useState<UiError | null>(null);
  const [expired, setExpired] = useState(false);
  const [ambiguous, setAmbiguous] = useState(false);

  const canApply = role === "owner" || role === "editor";

  /**
   * Stale-response guard. Every preview-scoped async result carries the generation it was started
   * under; a late reply from an older preview can never overwrite newer state.
   */
  const generation = useRef(0);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const [accountPage, payeePage] = await Promise.all([
          apiClient.get<Paged<Account>>("/api/v1/accounts?is_archived=false&limit=200"),
          apiClient.get<Paged<Payee>>("/api/v1/payees?limit=200&offset=0"),
        ]);
        if (!active) return;
        setAccounts(accountPage.items);
        setPayees(payeePage.items);
      } catch (loadError) {
        if (active) onError(loadError);
      }
    })();
    return () => {
      active = false;
    };
  }, [onError]);

  const loadItems = useCallback(
    async (previewId: string, nextOffset: number, token: number) => {
      setLoadingItems(true);
      try {
        const page = await fetchCategorizationPreviewItems(previewId, {
          limit: REVIEW_PAGE_SIZE,
          offset: nextOffset,
        });
        if (token !== generation.current) return;
        setItems(page.items);
        setTotal(page.page.total);
        setOffset(nextOffset);
      } catch (loadError) {
        if (token !== generation.current) return;
        if (loadError instanceof ApiClientError && loadError.status === 410) setExpired(true);
        else setError(toUiError(loadError));
      } finally {
        if (token === generation.current) setLoadingItems(false);
      }
    },
    [],
  );

  const createPreview = useCallback(async () => {
    if (creating) return;
    const token = generation.current + 1;
    generation.current = token;
    setCreating(true);
    setError(null);
    setExpired(false);
    setAmbiguous(false);
    setApplyResponse(null);
    setAttempt(null);
    setSelected(new Set());
    setItems([]);
    setTotal(0);
    setOffset(0);
    try {
      const created = await createCategorizationPreview(buildSelection(filters));
      if (token !== generation.current) return;
      setHeader(created);
      setStage("review");
      // The creation itself is done here; the first page load reports through its own flag, so the
      // Create button never stays stuck behind a slow items request.
      setCreating(false);
      await loadItems(created.id, 0, token);
      return;
    } catch (createError) {
      if (token !== generation.current) return;
      setError(toUiError(createError));
    }
    if (token === generation.current) setCreating(false);
  }, [creating, filters, loadItems]);

  const eligible = useMemo(() => items.filter((item) => item.status === "matched"), [items]);

  const resultsById = useMemo(() => {
    const map = new Map<string, CategorizationApplyItemResult>();
    for (const result of applyResponse?.results ?? []) map.set(result.item_id, result);
    return map;
  }, [applyResponse]);

  const toggle = useCallback(
    (itemId: string) => {
      setSelected((current) => {
        const next = new Set(current);
        if (next.has(itemId)) next.delete(itemId);
        else if (next.size < MAX_APPLY_ITEMS) next.add(itemId);
        else return current;
        return next;
      });
      setConfirming(false);
    },
    [],
  );

  const selectPage = useCallback(() => {
    setSelected((current) => {
      const next = new Set(current);
      for (const item of eligible) {
        if (next.size >= MAX_APPLY_ITEMS) break;
        next.add(item.id);
      }
      return next;
    });
    setConfirming(false);
  }, [eligible]);

  const clearSelection = useCallback(() => {
    setSelected(new Set());
    setConfirming(false);
  }, []);

  /**
   * A pending idempotency key belongs to exactly one selection. Deriving it keeps the rule in one
   * place: a retry is offered only while the selection still matches the attempt that failed.
   */
  const pendingAttempt = useMemo(
    () => (header ? retainAttemptForSelection(attempt, header.id, [...selected]) : null),
    [attempt, header, selected],
  );

  const runApply = useCallback(
    async (reuse: ApplyAttempt | null) => {
      if (!header || applying) return;
      const itemIds = [...selected];
      if (itemIds.length === 0 || itemIds.length > MAX_APPLY_ITEMS) return;
      const active = reuse ?? nextApplyAttempt(pendingAttempt, header.id, itemIds);
      setAttempt(active);
      setApplying(true);
      setConfirming(false);
      setError(null);
      setAmbiguous(false);
      const token = generation.current;
      try {
        const response = await applyCategorizationPreview(
          header.id,
          active.itemIds,
          active.idempotencyKey,
        );
        if (token !== generation.current) return;
        setApplyResponse(response);
        setAttempt({ ...active, state: "done" });
        setStage("results");
      } catch (applyError) {
        if (token !== generation.current) return;
        if (isAmbiguous(applyError)) {
          // Never claim nothing happened: the backend may already have committed the operation.
          setAttempt({ ...active, state: "ambiguous" });
          setAmbiguous(true);
          return;
        }
        setAttempt({ ...active, state: "idle" });
        if (applyError instanceof ApiClientError && applyError.status === 410) setExpired(true);
        else setError(toUiError(applyError));
      } finally {
        if (token === generation.current) setApplying(false);
      }
    },
    [applying, header, pendingAttempt, selected],
  );

  const startOver = useCallback(() => {
    generation.current += 1;
    setStage("select");
    setHeader(null);
    setItems([]);
    setTotal(0);
    setOffset(0);
    setSelected(new Set());
    setApplyResponse(null);
    setAttempt(null);
    setError(null);
    setExpired(false);
    setAmbiguous(false);
  }, []);

  const selectedCount = selected.size;
  const atLimit = selectedCount >= MAX_APPLY_ITEMS;
  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + items.length, total);

  return (
    <section className="screen categorization-review">
      <header className="panel-heading">
        <div>
          <span className="kicker">Категоризация</span>
          <h2>Проверка предложений</h2>
          <p>
            Составьте список предложений, проверьте их и примените только выбранные. Ничего не
            применяется автоматически.
          </p>
        </div>
      </header>

      {!roleLoading && role === "viewer" ? (
        <div className="notice notice--info" role="status">
          <span>У вас доступ только на чтение.</span>
        </div>
      ) : null}
      {!roleLoading && role === null ? (
        <div className="notice notice--warning" role="status">
          <span>Не удалось подтвердить права. Применение отключено.</span>
        </div>
      ) : null}

      {error ? (
        <div className="categorization-inline-error" role="alert">
          <strong>{error.message}</strong>
          <code>{error.code}</code>
          {error.requestId ? <small>Запрос {error.requestId}</small> : null}
        </div>
      ) : null}

      {expired ? (
        <div className="notice notice--warning" role="status">
          <span>
            Список устарел. Составьте новый, чтобы увидеть актуальные операции и правила.
          </span>
          <button className="primary-button" onClick={startOver} type="button">
            Составить новый список
          </button>
        </div>
      ) : null}

      {stage === "select" || !header ? (
        <CategorizationReviewFilters
          accounts={accounts}
          busy={creating}
          filters={filters}
          onChange={setFilters}
          onSubmit={() => void createPreview()}
          payees={payees}
        />
      ) : null}

      {header && stage !== "select" ? (
        <>
          <CategorizationPreviewSummary header={header} />

          {stage === "review" ? (
            <div className="review-toolbar">
              <div className="review-toolbar__selection">
                <button
                  className="text-button"
                  disabled={!canApply || eligible.length === 0 || applying}
                  onClick={selectPage}
                  type="button"
                >
                  Выбрать предложения на странице
                </button>
                <button
                  className="text-button"
                  disabled={selectedCount === 0 || applying}
                  onClick={clearSelection}
                  type="button"
                >
                  Снять выбор
                </button>
                <button className="text-button" disabled={applying} onClick={startOver} type="button">
                  Составить другой список
                </button>
                <span aria-live="polite">Выбрано {selectedCount} из не более {MAX_APPLY_ITEMS}</span>
              </div>
              {atLimit ? (
                <p className="notice notice--info" role="status">
                  За один раз применяется не более {MAX_APPLY_ITEMS} предложений. Остальные можно
                  применить следующей партией.
                </p>
              ) : null}
              {canApply ? (
                <button
                  className="primary-button"
                  disabled={selectedCount === 0 || applying}
                  onClick={() => setConfirming(true)}
                  type="button"
                >
                  Применить выбранные ({selectedCount})
                </button>
              ) : null}
            </div>
          ) : null}

          {confirming ? (
            <div className="notice notice--info review-confirm" role="status">
              <p>Применить категории к {selectedCount} операциям?</p>
              <p>
                Будут применены только выбранные предложения. Операции, изменившиеся после
                составления списка, будут безопасно отклонены.
              </p>
              <div className="review-confirm__actions">
                <button
                  className="primary-button"
                  disabled={applying}
                  onClick={() => void runApply(null)}
                  type="button"
                >
                  Подтвердить и применить
                </button>
                <button className="text-button" onClick={() => setConfirming(false)} type="button">
                  Отмена
                </button>
              </div>
            </div>
          ) : null}

          {ambiguous && pendingAttempt && pendingAttempt.state === "ambiguous" ? (
            <div className="notice notice--warning" role="status">
              <span>
                Ответ сервера не получен, поэтому результат неизвестен — часть операций уже могла
                быть применена. Повторите тем же запросом: сервер вернёт исходный результат и не
                применит ничего дважды.
              </span>
              <button
                className="primary-button"
                disabled={applying}
                onClick={() => void runApply(pendingAttempt)}
                type="button"
              >
                Повторить безопасно
              </button>
            </div>
          ) : null}

          {applyResponse ? (
            <CategorizationApplyResults
              onNewPreview={startOver}
              onReviewRemaining={() => setStage("review")}
              response={applyResponse}
            />
          ) : null}

          <CategorizationReviewTable
            accounts={accounts}
            items={items}
            loading={loadingItems}
            onToggle={toggle}
            pending={applying}
            results={resultsById}
            selected={selected}
          />

          <div className="review-pagination">
            <button
              className="text-button"
              disabled={offset === 0 || loadingItems}
              onClick={() => void loadItems(header.id, Math.max(0, offset - REVIEW_PAGE_SIZE), generation.current)}
              type="button"
            >
              Назад
            </button>
            <span>
              {pageStart}–{pageEnd} из {total}
            </span>
            <button
              className="text-button"
              disabled={offset + REVIEW_PAGE_SIZE >= total || loadingItems}
              onClick={() => void loadItems(header.id, offset + REVIEW_PAGE_SIZE, generation.current)}
              type="button"
            >
              Вперёд
            </button>
          </div>
        </>
      ) : null}
    </section>
  );
}
