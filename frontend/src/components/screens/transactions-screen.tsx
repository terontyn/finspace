"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { formatMoney } from "@/lib/money";
import type {
  Account,
  AuditEntry,
  Category,
  Currency,
  Paged,
  Transaction,
  TransactionStatus,
  TransactionType,
} from "@/types/finance";

interface TransactionsScreenProps {
  onError: (error: unknown) => void;
}

interface TransactionForm {
  occurredAt: string;
  transactionType: "income" | "expense" | "transfer";
  amount: string;
  currency: Currency;
  accountId: string;
  targetAccountId: string;
  categoryId: string;
  counterparty: string;
  description: string;
  status: "draft" | "confirmed";
}

const typeLabels: Record<TransactionType, string> = {
  income: "Доход",
  expense: "Расход",
  transfer: "Перевод",
  refund: "Возврат",
  adjustment: "Корректировка",
};

const statusLabels: Record<TransactionStatus, string> = {
  draft: "Черновик",
  confirmed: "Подтверждена",
  reconciled: "Сверена",
  cancelled: "Отменена",
};

function initialForm(): TransactionForm {
  const now = new Date();
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
  return {
    occurredAt: now.toISOString().slice(0, 16),
    transactionType: "expense",
    amount: "",
    currency: "RUB",
    accountId: "",
    targetAccountId: "",
    categoryId: "",
    counterparty: "",
    description: "",
    status: "confirmed",
  };
}

export function TransactionsScreen({ onError }: TransactionsScreenProps) {
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [form, setForm] = useState<TransactionForm>(initialForm);
  const [editing, setEditing] = useState<Transaction | null>(null);
  const [history, setHistory] = useState<{ transaction: Transaction; items: AuditEntry[] } | null>(null);
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const limit = 10;

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (typeFilter) params.set("transaction_type", typeFilter);
    if (statusFilter) params.set("status", statusFilter);
    if (appliedSearch) params.set("search", appliedSearch);
    return params.toString();
  }, [appliedSearch, offset, statusFilter, typeFilter]);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const [transactionResult, accountResult, categoryResult] = await Promise.all([
        apiClient.get<Paged<Transaction>>(`/api/v1/transactions?${query}`),
        apiClient.get<Paged<Account>>("/api/v1/accounts?is_archived=false&limit=200"),
        apiClient.get<Paged<Category>>("/api/v1/categories?is_archived=false&limit=500"),
      ]);
      setTransactions(transactionResult.items);
      setTotal(transactionResult.page.total);
      setAccounts(accountResult.items);
      setCategories(categoryResult.items);
    } catch (error) {
      onError(error);
    } finally {
      setIsLoading(false);
    }
  }, [onError, query]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  function selectAccount(accountId: string) {
    const account = accounts.find((item) => item.id === accountId);
    setForm({ ...form, accountId, currency: account?.currency ?? form.currency });
  }

  function resetForm() {
    setEditing(null);
    setForm(initialForm());
  }

  function startEdit(transaction: Transaction) {
    const date = new Date(transaction.occurred_at);
    date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
    if (!["income", "expense", "transfer"].includes(transaction.transaction_type)) return;
    setEditing(transaction);
    setForm({
      occurredAt: date.toISOString().slice(0, 16),
      transactionType: transaction.transaction_type as TransactionForm["transactionType"],
      amount: transaction.amount,
      currency: transaction.currency,
      accountId: transaction.account.id,
      targetAccountId: transaction.target_account?.id ?? "",
      categoryId: transaction.category?.id ?? "",
      counterparty: transaction.counterparty ?? "",
      description: transaction.description ?? "",
      status: transaction.status === "draft" ? "draft" : "confirmed",
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setIsSaving(true);
    const payload = {
      occurred_at: new Date(form.occurredAt).toISOString(),
      transaction_type: form.transactionType,
      amount: form.amount,
      currency: form.currency,
      account_id: form.accountId,
      target_account_id: form.transactionType === "transfer" ? form.targetAccountId : null,
      category_id: form.transactionType === "transfer" ? null : form.categoryId || null,
      counterparty: form.counterparty || null,
      description: form.description || null,
      status: form.status,
    };
    try {
      if (editing) {
        await apiClient.patch<Transaction>(`/api/v1/transactions/${editing.id}`, {
          ...payload,
          version: editing.version,
        });
      } else {
        await apiClient.post<Transaction>("/api/v1/transactions", payload);
      }
      resetForm();
      await load();
    } catch (error) {
      onError(error);
    } finally {
      setIsSaving(false);
    }
  }

  async function cancel(transaction: Transaction) {
    try {
      await apiClient.post<Transaction>(`/api/v1/transactions/${transaction.id}/cancel`, {
        version: transaction.version,
      });
      await load();
    } catch (error) {
      onError(error);
    }
  }

  async function showHistory(transaction: Transaction) {
    try {
      const result = await apiClient.get<Paged<AuditEntry>>(
        `/api/v1/transactions/${transaction.id}/history`,
      );
      setHistory({ transaction, items: result.items });
    } catch (error) {
      onError(error);
    }
  }

  const visibleCategories = categories.filter(
    (category) =>
      category.category_type === form.transactionType || category.category_type === "both",
  );

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Финансовый журнал</span>
          <h1>Операции</h1>
          <p>Доходы, расходы и переводы с оптимистичной блокировкой.</p>
        </div>
        <button className="secondary-button" type="button" onClick={() => void load()}>
          Повторить загрузку
        </button>
      </header>

      <form className="form-panel transaction-form" onSubmit={(event) => void save(event)}>
        <div className="panel-heading">
          <div>
            <span className="kicker">{editing ? "Редактирование" : "Новая запись"}</span>
            <h2>{editing ? "Изменить операцию" : "Добавить операцию"}</h2>
          </div>
          {editing ? <button className="text-button" type="button" onClick={resetForm}>Отмена</button> : null}
        </div>
        <div className="transaction-form-grid">
          <label>
            Дата и время
            <input required type="datetime-local" value={form.occurredAt} onChange={(event) => setForm({ ...form, occurredAt: event.target.value })} />
          </label>
          <label>
            Тип
            <select value={form.transactionType} onChange={(event) => {
              const account = accounts.find((a) => a.id === form.accountId);
              setForm({ ...form, transactionType: event.target.value as TransactionForm["transactionType"], categoryId: "", targetAccountId: "", currency: account?.currency ?? form.currency });
            }}>
              <option value="income">Доход</option><option value="expense">Расход</option><option value="transfer">Перевод</option>
            </select>
          </label>
          <label>
            Сумма
            <input required inputMode="decimal" pattern="\d+(\.\d{1,4})?" value={form.amount} onChange={(event) => setForm({ ...form, amount: event.target.value })} />
          </label>
          <label>
            Счёт
            <select required value={form.accountId} onChange={(event) => selectAccount(event.target.value)}>
              <option value="">Выберите счёт</option>
              {accounts.map((account) => <option key={account.id} value={account.id}>{account.name} · {account.currency}</option>)}
            </select>
          </label>
          {form.transactionType === "transfer" ? (
            <label>
              Счёт назначения
              <select required value={form.targetAccountId} onChange={(event) => setForm({ ...form, targetAccountId: event.target.value })}>
                <option value="">Выберите счёт</option>
                {accounts.filter((account) => account.id !== form.accountId).map((account) => <option key={account.id} value={account.id}>{account.name} · {account.currency}</option>)}
              </select>
            </label>
          ) : (
            <label>
              Категория
              <select value={form.categoryId} onChange={(event) => setForm({ ...form, categoryId: event.target.value })}>
                <option value="">Без категории</option>
                {visibleCategories.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
              </select>
            </label>
          )}
          <label>
            Контрагент
            <input value={form.counterparty} onChange={(event) => setForm({ ...form, counterparty: event.target.value })} />
          </label>
          <label>
            Статус
            <select value={form.status} onChange={(event) => setForm({ ...form, status: event.target.value as TransactionForm["status"] })}>
              <option value="confirmed">Подтверждена</option><option value="draft">Черновик</option>
            </select>
          </label>
          <label className="span-two">
            Описание
            <input value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} />
          </label>
        </div>
        <button type="submit" disabled={isSaving}>{isSaving ? "Сохраняем…" : editing ? "Сохранить" : "Добавить операцию"}</button>
      </form>

      <div className="panel table-panel">
        <form className="filters" onSubmit={(event) => { event.preventDefault(); setOffset(0); setAppliedSearch(search); }}>
          <select aria-label="Фильтр по типу" value={typeFilter} onChange={(event) => { setOffset(0); setTypeFilter(event.target.value); }}>
            <option value="">Все типы</option>
            {Object.entries(typeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <select aria-label="Фильтр по статусу" value={statusFilter} onChange={(event) => { setOffset(0); setStatusFilter(event.target.value); }}>
            <option value="">Все статусы</option>
            {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <input aria-label="Поиск" placeholder="Контрагент или описание" value={search} onChange={(event) => setSearch(event.target.value)} />
          <button className="secondary-button" type="submit">Найти</button>
        </form>

        <div className="table-scroll">
          <table>
            <thead><tr><th>Дата</th><th>Тип</th><th>Сумма</th><th>Валюта</th><th>Счёт</th><th>Счёт назначения</th><th>Категория</th><th>Контрагент</th><th>Описание</th><th>Статус</th><th /></tr></thead>
            <tbody>
              {transactions.map((transaction) => (
                <tr key={transaction.id}>
                  <td>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(new Date(transaction.occurred_at))}</td>
                  <td><span className={`type-chip type-chip--${transaction.transaction_type}`}>{typeLabels[transaction.transaction_type]}</span></td>
                  <td className="amount-cell">{formatMoney(transaction.amount, transaction.currency)}</td>
                  <td>{transaction.currency}</td><td>{transaction.account.name}</td><td>{transaction.target_account?.name ?? "—"}</td><td>{transaction.category?.name ?? "—"}</td><td>{transaction.counterparty ?? "—"}</td><td>{transaction.description ?? "—"}</td>
                  <td><span className={`status-chip status-chip--${transaction.status}`}>{statusLabels[transaction.status]}</span></td>
                  <td><div className="table-actions"><button className="text-button" type="button" onClick={() => startEdit(transaction)}>Изменить</button><button className="text-button" type="button" onClick={() => void showHistory(transaction)}>История</button>{transaction.status !== "cancelled" ? <button className="text-button text-button--danger" type="button" onClick={() => void cancel(transaction)}>Отменить</button> : null}</div></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!isLoading && !transactions.length ? <div className="empty-state">Операции не найдены.</div> : null}
          {isLoading ? <div className="empty-state">Загружаем операции…</div> : null}
        </div>
        <div className="pagination">
          <span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} из ${total}` : "0 операций"}</span>
          <div><button className="secondary-button" type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>Назад</button><button className="secondary-button" type="button" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>Дальше</button></div>
        </div>
      </div>

      {history ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setHistory(null)}>
          <section className="history-panel" role="dialog" aria-modal="true" aria-label="История операции" onMouseDown={(event) => event.stopPropagation()}>
            <div className="panel-heading"><div><span className="kicker">Audit log</span><h2>История операции</h2><p>{history.transaction.description ?? typeLabels[history.transaction.transaction_type]}</p></div><button className="text-button" type="button" onClick={() => setHistory(null)}>Закрыть</button></div>
            <ol className="history-list">{history.items.map((entry) => <li key={entry.id}><span className="history-dot" /><div><strong>{entry.action}</strong><span>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "medium" }).format(new Date(entry.created_at))}</span>{entry.request_id ? <code>{entry.request_id}</code> : null}</div></li>)}</ol>
          </section>
        </div>
      ) : null}
    </section>
  );
}
