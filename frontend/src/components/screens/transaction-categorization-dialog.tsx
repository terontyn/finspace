"use client";

import { useState } from "react";

import { ActionDialog } from "@/components/ui/action-dialog";
import { categorizationUiError, type CategorizationUiError } from "@/lib/categorization";
import { ApiClientError, apiClient } from "@/lib/api-client";
import type { CategorizationApplyResult, CategorizationPreview } from "@/types/categorization";
import type { Transaction } from "@/types/finance";

import { transactionPartyName } from "./transaction-presenters";

interface TransactionCategorizationDialogProps {
  canApply: boolean;
  onApplied: (transaction: Transaction) => Promise<void> | void;
  onClose: () => void;
  onError: (error: unknown) => void;
  roleLoading: boolean;
  transaction: Transaction;
}

function DialogError({ error }: { error: CategorizationUiError }) {
  return <div className="categorization-inline-error" role="alert"><strong>{error.message}</strong><code>{error.code}</code>{error.requestId ? <small>Запрос {error.requestId}</small> : null}</div>;
}

export function TransactionCategorizationDialog({ canApply, onApplied, onClose, onError, roleLoading, transaction }: TransactionCategorizationDialogProps) {
  const [preview, setPreview] = useState<CategorizationPreview | null>(null);
  const [issue, setIssue] = useState<CategorizationUiError | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [staleVersion, setStaleVersion] = useState(false);
  const [refreshNotice, setRefreshNotice] = useState<CategorizationUiError | null>(null);
  const hasExistingCategorization = Boolean(transaction.category || transaction.splits.length);
  const isTransfer = transaction.transaction_type === "transfer";
  const canPreview = !hasExistingCategorization && !isTransfer;

  // Reset during render (React's documented prop-change adjustment) instead of in an effect: the
  // parent replaces this transaction after every apply, and an effect-based reset would race the
  // result message it is supposed to leave alone.
  const [seen, setSeen] = useState({ id: transaction.id, version: transaction.version });
  if (seen.id !== transaction.id || seen.version !== transaction.version) {
    setSeen({ id: transaction.id, version: transaction.version });
    setPreview(null);
    setIssue(null);
    setStaleVersion(false);
    if (seen.id !== transaction.id) {
      setOutcome(null);
      setRefreshNotice(null);
    }
  }

  function report(error: unknown) {
    setIssue(categorizationUiError(error));
    if (!(error instanceof ApiClientError)) onError(error);
  }

  async function refreshStaleTransaction(conflict: CategorizationUiError): Promise<void> {
    try {
      const fresh = await apiClient.get<Transaction>(`/api/v1/transactions/${transaction.id}`);
      await onApplied(fresh);
      setRefreshNotice({ ...conflict, message: `${conflict.message} Операция перечитана: версия ${fresh.version}. Выполните предпросмотр заново.` });
    } catch {
      setStaleVersion(true);
      setIssue(conflict);
    }
  }

  async function requestPreview() {
    if (!canPreview) return;
    setPreviewing(true);
    setIssue(null);
    setOutcome(null);
    setRefreshNotice(null);
    try {
      setPreview(await apiClient.post<CategorizationPreview>("/api/v1/categorization-rules/preview", { transaction_id: transaction.id }));
    } catch (error) {
      report(error);
    } finally {
      setPreviewing(false);
    }
  }

  async function apply() {
    if (!canApply || !preview?.matched || !preview.rule || !preview.category) return;
    setApplying(true);
    setIssue(null);
    setOutcome(null);
    setRefreshNotice(null);
    try {
      const result = await apiClient.post<CategorizationApplyResult>(`/api/v1/transactions/${transaction.id}/apply-categorization`, { version: transaction.version });
      if (result.applied) {
        await onApplied(result.transaction);
        setOutcome(`Категория «${result.category?.name ?? "—"}» применена. Операция обновлена до версии ${result.transaction.version}.`);
        return;
      }
      setPreview(null);
      await onApplied(result.transaction);
      setOutcome(result.reason === "already_categorized" ? "Операция уже получила категорию или разделение. Правила ничего не перезаписали." : "Подходящее правило больше не найдено. Выполните новый предпросмотр.");
    } catch (error) {
      const conflict = error instanceof ApiClientError ? error.code : null;
      if (conflict === "CATEGORIZATION_RULE_CHANGED" || conflict === "VERSION_CONFLICT") setPreview(null);
      // CATEGORIZATION_RULE_CHANGED leaves the transaction current, so clearing the preview is the
      // whole recovery. VERSION_CONFLICT means the transaction itself is stale: the known version
      // must never be reused, so re-read the record and hand it to the parent. If the re-read fails
      // the dialog refuses to apply at all instead of looping on a version the backend rejects.
      if (conflict === "VERSION_CONFLICT") {
        await refreshStaleTransaction(categorizationUiError(error));
        return;
      }
      report(error);
    } finally {
      setApplying(false);
    }
  }

  return <ActionDialog description="Предпросмотр ничего не изменяет. Применение выполняется только отдельной командой с текущей версией операции." eyebrow="Явная категоризация" onClose={onClose} title={transactionPartyName(transaction)}>
    <div className="transaction-categorization-dialog">
      <dl className="categorization-transaction-facts"><div><dt>Версия</dt><dd>v{transaction.version}</dd></div><div><dt>Получатель</dt><dd>{transaction.payee?.name ?? "Не назначен"}</dd></div><div><dt>Исходный контрагент</dt><dd>{transaction.counterparty ?? "Не указан"}</dd></div></dl>
      <p className="categorization-separation-note">Получатель и исходный контрагент проверяются независимо. Правила не выводят получателя из текста контрагента.</p>
      {hasExistingCategorization ? <div className="categorization-guard" role="status"><strong>Категоризация уже задана</strong><span>{transaction.splits.length ? `Операция разделена на ${transaction.splits.length} части.` : `Назначена категория «${transaction.category?.name}».`} Правила не используются для перезаписи.</span></div> : isTransfer ? <div className="categorization-guard" role="status"><strong>Переводы не категоризируются правилами</strong><span>Для этой операции совпадение не ожидается, поэтому preview не выполняется.</span></div> : <>
        <div className="categorization-preview-command"><div><strong>Найти подходящее правило</strong><span>Backend проверит активные правила в детерминированном порядке.</span></div><button className="secondary-button" disabled={previewing || applying} onClick={() => void requestPreview()} type="button">{previewing ? "Проверяем…" : preview ? "Повторить предпросмотр" : "Предпросмотр"}</button></div>
        {preview ? preview.matched && preview.rule && preview.category ? <section className="categorization-preview-result" aria-label="Найденное правило"><span className="status-chip status-chip--confirmed">Совпадение найдено</span><div><span>Правило</span><strong>{preview.rule.name}</strong></div><div><span>Целевая категория</span><strong>{preview.category.name}</strong></div><small>Приоритет {preview.rule.priority} · версия правила {preview.rule.version}</small></section> : <div className="categorization-no-match" role="status"><strong>Подходящего правила нет</strong><span>Операция не изменена.</span></div> : null}
        {preview?.matched && !canApply && !roleLoading ? <div className="categorization-viewer-note" role="status">Режим просмотра: результат preview доступен, применение — только редактору или владельцу.</div> : null}
      </>}
      {staleVersion ? <div className="categorization-guard" role="status"><strong>Версия операции устарела</strong><span>Не удалось перечитать операцию. Закройте диалог, обновите список и откройте операцию заново.</span></div> : null}
      {refreshNotice ? <DialogError error={refreshNotice}/> : null}
      {issue ? <DialogError error={issue}/> : null}
      {outcome ? <div className="categorization-success" role="status">{outcome}</div> : null}
    </div>
    <footer><button className="secondary-button" disabled={applying} onClick={onClose} type="button">Закрыть</button>{canPreview && preview?.matched && canApply && !staleVersion ? <button className="primary-button" disabled={applying || previewing} onClick={() => void apply()} type="button">{applying ? "Применяем…" : "Применить категорию"}</button> : null}</footer>
  </ActionDialog>;
}
