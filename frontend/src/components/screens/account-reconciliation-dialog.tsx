"use client";

import { useState } from "react";

import { ApiClientError, apiClient } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type {
  Account,
  AccountReconciliation,
  AccountReconciliationPreview,
} from "@/types/finance";

import { transactionTypeLabels } from "./transaction-presenters";

interface AccountReconciliationDialogProps {
  account: Account;
  onClose: () => void;
  onConfirmed: (result: AccountReconciliation) => Promise<void> | void;
  onError: (error: unknown) => void;
  timezone: string;
}

function localIsoDate(timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    month: "2-digit",
    timeZone: timezone,
    year: "numeric",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

export function isZeroMoney(value: string): boolean {
  return /^-?0(?:\.0+)?$/.test(value);
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `account-reconciliation-${crypto.randomUUID()}`;
  }
  return `account-reconciliation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function AccountReconciliationDialog({
  account,
  onClose,
  onConfirmed,
  onError,
  timezone,
}: AccountReconciliationDialogProps) {
  const [statementDate, setStatementDate] = useState(() => localIsoDate(timezone));
  const [statementBalance, setStatementBalance] = useState("");
  const [preview, setPreview] = useState<AccountReconciliationPreview | null>(null);
  const [confirmed, setConfirmed] = useState<AccountReconciliation | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);

  function resetPreview() {
    setPreview(null);
    setConfirmed(null);
    setConflict(null);
    setIdempotencyKey(null);
  }

  async function createPreview(event: React.FormEvent) {
    event.preventDefault();
    setIsPreviewing(true);
    setConflict(null);
    setConfirmed(null);
    try {
      const result = await apiClient.post<AccountReconciliationPreview>(
        `/api/v1/accounts/${account.id}/reconciliation/preview`,
        {
          account_version: account.version,
          currency: account.currency,
          statement_balance: statementBalance,
          statement_date: statementDate,
        },
      );
      setPreview(result);
      setIdempotencyKey(newIdempotencyKey());
    } catch (error) {
      if (error instanceof ApiClientError && error.status === 409) {
        setConflict("Счёт или операции изменились. Закройте сверку, обновите страницу и повторите preview.");
      } else {
        onError(error);
      }
    } finally {
      setIsPreviewing(false);
    }
  }

  async function confirm() {
    if (!preview || !idempotencyKey || !isZeroMoney(preview.difference)) return;
    setIsConfirming(true);
    setConflict(null);
    try {
      const result = await apiClient.post<AccountReconciliation>(
        `/api/v1/accounts/${account.id}/reconciliation/confirm`,
        {
          account_version: preview.account_version,
          currency: preview.currency,
          idempotency_key: idempotencyKey,
          preview_token: preview.preview_token,
          statement_balance: preview.statement_balance,
          statement_date: preview.statement_date,
        },
      );
      setConfirmed(result);
      await onConfirmed(result);
    } catch (error) {
      if (error instanceof ApiClientError && error.status === 409) {
        setConflict("Подтверждение не выполнено: данные изменились или этот preview устарел. Автоповтор отключён — сформируйте новый preview.");
      } else {
        onError(error);
      }
    } finally {
      setIsConfirming(false);
    }
  }

  return <div className="reconciliation-backdrop" role="presentation">
    <section aria-labelledby="reconciliation-title" aria-modal="true" className="reconciliation-dialog" role="dialog">
      <header><div><span className="kicker">Account reconciliation</span><h2 id="reconciliation-title">Сверить «{account.name}»</h2><small>Баланс Finspace сравнивается с выпиской на конец локального дня.</small></div><button aria-label="Закрыть сверку" className="drawer-close" disabled={isConfirming} onClick={onClose} type="button">×</button></header>

      {confirmed ? <div className="reconciliation-success"><span>✓</span><div><strong>Сверка подтверждена</strong><p>{confirmed.transaction_ids.length} операций отмечено как сверенные. Данные счёта и история обновлены.</p></div><button className="primary-button" onClick={onClose} type="button">Готово</button></div> : <>
        <form className="reconciliation-form" onSubmit={(event) => void createPreview(event)}>
          <label>Дата выписки<input max="9999-12-31" onChange={(event) => { setStatementDate(event.target.value); resetPreview(); }} required type="date" value={statementDate}/></label>
          <label>Баланс по выписке<div className="reconciliation-balance-input"><input inputMode="decimal" onChange={(event) => { setStatementBalance(event.target.value); resetPreview(); }} placeholder="0,00" required step="0.0001" type="number" value={statementBalance}/><strong>{account.currency}</strong></div></label>
          <button className="secondary-button" disabled={isPreviewing || !statementBalance || !statementDate} type="submit">{isPreviewing ? "Считаем…" : preview ? "Обновить preview" : "Сформировать preview"}</button>
        </form>

        {preview ? <section className="reconciliation-preview" aria-label="Результат предварительной сверки">
          <div className="reconciliation-totals"><article><span>Finspace</span><strong>{formatMoney(preview.calculated_balance, preview.currency)}</strong></article><article><span>Банк</span><strong>{formatMoney(preview.statement_balance, preview.currency)}</strong></article><article><span>Разница: Банк − Finspace</span><strong className={`money--${moneyTone(preview.difference)}`}>{formatMoney(preview.difference, preview.currency)}</strong></article></div>
          {!isZeroMoney(preview.difference) ? <div className="reconciliation-warning"><strong>Есть расхождение</strong><p>Подтверждение заблокировано. Проверьте операции или создайте отдельную явную корректировку, затем сформируйте новый preview. Автоматическая корректировка не создаётся.</p></div> : <div className="reconciliation-match"><strong>Баланс совпадает</strong><span>Можно атомарно подтвердить сверку.</span></div>}
          <div className="reconciliation-candidates"><div><strong>Операции к сверке</strong><span>{preview.transactions.length}</span></div>{preview.transactions.length ? <ul>{preview.transactions.map((transaction) => <li key={transaction.id}><div><strong>{transaction.counterparty ?? transactionTypeLabels[transaction.transaction_type]}</strong><span>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: timezone }).format(new Date(transaction.occurred_at))} · {transaction.description ?? transactionTypeLabels[transaction.transaction_type]}</span></div><b className={`money--${moneyTone(transaction.signed_amount)}`}>{formatMoney(transaction.signed_amount, transaction.currency)}</b></li>)}</ul> : <p>Новых подтверждённых операций до этой даты нет.</p>}</div>
        </section> : null}

        {conflict ? <div className="reconciliation-conflict" role="alert">{conflict}</div> : null}
        <footer><button className="secondary-button" disabled={isConfirming} onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={!preview || !isZeroMoney(preview.difference) || isConfirming} onClick={() => void confirm()} type="button">{isConfirming ? "Подтверждаем…" : "Подтвердить сверку"}</button></footer>
      </>}
    </section>
  </div>;
}
