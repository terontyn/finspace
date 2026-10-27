"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiClientError, apiClient } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type { Account, AccountBalance, AccountReconciliation, Paged, Transaction, TransactionStatus, TransactionType } from "@/types/finance";

import { AccountDrawer, accountTypeLabels, accountTypeSymbols } from "./account-drawer";
import { accountArchiveMutation, accountDeleteMutation, accountFormFromRecord, accountMutation, initialAccountForm, type AccountForm } from "./account-form";
import { AccountReconciliationDialog } from "./account-reconciliation-dialog";
import { AccountTransactionList } from "./account-transaction-list";
import { currentMonthPeriod } from "./dashboard-data";

interface AccountDetailsScreenProps {
  accountId: string;
  onError: (error: unknown) => void;
  timezone: string;
}

const transactionLimit = 10;

export function AccountDetailsScreen({ accountId, onError, timezone }: AccountDetailsScreenProps) {
  const [account, setAccount] = useState<Account | null>(null);
  const [balance, setBalance] = useState<AccountBalance | null>(null);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [reconciliations, setReconciliations] = useState<AccountReconciliation[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [period, setPeriod] = useState<"month" | "all">("month");
  const [typeFilter, setTypeFilter] = useState<"" | TransactionType>("");
  const [statusFilter, setStatusFilter] = useState<"" | TransactionStatus>("");
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [reconciliationOpen, setReconciliationOpen] = useState(false);
  const [form, setForm] = useState<AccountForm>(() => initialAccountForm());
  const month = useMemo(() => currentMonthPeriod(new Date(), timezone), [timezone]);

  const transactionQuery = useMemo(() => {
    const params = new URLSearchParams({
      account_id: accountId,
      limit: String(transactionLimit),
      offset: String(offset),
    });
    if (period === "month") {
      params.set("date_from", month.dateFrom);
      params.set("date_to", month.dateTo);
    }
    if (typeFilter) params.set("transaction_type", typeFilter);
    if (statusFilter) params.set("status", statusFilter);
    if (appliedSearch) params.set("search", appliedSearch);
    return params.toString();
  }, [accountId, appliedSearch, month, offset, period, statusFilter, typeFilter]);

  const load = useCallback(async () => {
    setIsLoading(true);
    setLoadFailed(false);
    try {
      const [accountResult, balances, transactionResult, reconciliationResult] = await Promise.all([
        apiClient.get<Account>(`/api/v1/accounts/${accountId}`),
        apiClient.get<AccountBalance[]>("/api/v1/accounts/balances"),
        apiClient.get<Paged<Transaction>>(`/api/v1/transactions?${transactionQuery}`),
        apiClient.get<Paged<AccountReconciliation>>(`/api/v1/accounts/${accountId}/reconciliations?limit=10&offset=0`),
      ]);
      setAccount(accountResult);
      setBalance(balances.find((item) => item.account_id === accountId) ?? null);
      setTransactions(transactionResult.items);
      setReconciliations(reconciliationResult.items);
      setTotal(transactionResult.page.total);
      setNotFound(false);
    } catch (error) {
      setAccount(null);
      setBalance(null);
      setTransactions([]);
      setReconciliations([]);
      setTotal(0);
      if (error instanceof ApiClientError && error.status === 404) setNotFound(true);
      else {
        setLoadFailed(true);
        onError(error);
      }
    } finally {
      setIsLoading(false);
    }
  }, [accountId, onError, transactionQuery]);

  useEffect(() => { queueMicrotask(() => void load()); }, [load]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!account) return;
    setIsSaving(true);
    const mutation = accountMutation(form, account);
    try {
      const updated = await apiClient.patch<Account>(mutation.path, mutation.body);
      setAccount(updated);
      setDrawerOpen(false);
      await load();
    } catch (error) { onError(error); } finally { setIsSaving(false); }
  }

  async function setArchived(isArchived: boolean) {
    if (!account) return;
    const mutation = accountArchiveMutation(account, isArchived);
    try {
      setAccount(await apiClient.patch<Account>(mutation.path, mutation.body));
      await load();
    } catch (error) { onError(error); }
  }

  async function remove() {
    if (!account || !window.confirm(`Удалить счёт «${account.name}»?`)) return;
    const mutation = accountDeleteMutation(account);
    try {
      await apiClient.delete<Account>(mutation.path);
      window.location.assign("/accounts");
    } catch (error) { onError(error); }
  }

  if (isLoading && !account) return <div aria-label="Загружаем счёт" className="dashboard-skeleton">{Array.from({ length: 5 }, (_, index) => <i key={index}/>)}</div>;
  if (notFound) return <section className="panel account-detail-state"><span className="kicker">Счёт недоступен</span><h1>Счёт не найден</h1><p>Он удалён, находится в другом пространстве или ссылка устарела.</p><Link className="primary-button" href="/accounts">Вернуться к счетам</Link></section>;
  if (loadFailed || !account) return <section className="panel account-detail-state"><span className="kicker">Ошибка загрузки</span><h1>Не удалось открыть счёт</h1><p>Повторите запрос. Технические подробности показаны в безопасном уведомлении.</p><button className="primary-button" onClick={() => void load()} type="button">Повторить</button></section>;

  const currentBalance = balance?.balance ?? account.opening_balance;
  const lastActivity = transactions[0]?.occurred_at ?? account.updated_at;

  return <section>
    <nav aria-label="Хлебные крошки" className="breadcrumbs"><Link href="/accounts">Счета</Link><span>→</span><span>{account.name}</span></nav>
    <header className="screen-header account-detail-header"><div className="account-detail-title"><span className="account-type-symbol">{accountTypeSymbols[account.account_type]}</span><div><span className="kicker">{accountTypeLabels[account.account_type]} · {account.currency}</span><h1>{account.institution ? `${account.institution} · ${account.name}` : account.name}</h1><p>{account.description ?? "Операции и точный backend-баланс этого счёта."}</p></div></div><div className="screen-header-actions"><Link className="primary-button" href={`/transactions?new=1&account_id=${account.id}`}>＋ Операция</Link><button className="secondary-button" onClick={() => { setForm(accountFormFromRecord(account)); setDrawerOpen(true); }} type="button">Редактировать</button><button className="secondary-button" onClick={() => setReconciliationOpen(true)} type="button">Сверить счёт</button></div></header>

    <section aria-label="Сводка по счёту" className="account-summary-grid account-detail-summary">
      <article><span>Текущий баланс</span><strong className={`money--${moneyTone(currentBalance)}`}>{formatMoney(currentBalance, account.currency)}</strong><small>Рассчитан backend</small></article>
      <article><span>Начальный баланс</span><strong>{formatMoney(account.opening_balance, account.currency)}</strong><small>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: timezone }).format(new Date(account.opening_balance_at))}</small></article>
      {account.credit_limit ? <article><span>Кредитный лимит</span><strong>{formatMoney(account.credit_limit, account.currency)}</strong><small>Только для кредитной карты</small></article> : null}
      <article><span>Операций по фильтру</span><strong>{total}</strong><small>{period === "month" ? month.label : "За всё время"}</small></article>
      <article><span>Последняя активность</span><strong>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: timezone }).format(new Date(lastActivity))}</strong><small>{account.is_archived ? "Счёт в архиве" : "Активный счёт"}</small></article>
      <article><span>Последняя сверка</span><strong>{reconciliations[0] ? new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: timezone }).format(new Date(`${reconciliations[0].statement_date}T12:00:00Z`)) : "Не выполнялась"}</strong><small>{reconciliations[0] ? `${reconciliations[0].transaction_ids.length} операций` : "Сверьте счёт с выпиской"}</small></article>
    </section>

    <section className="panel account-detail-meta"><div><span>Организация</span><strong>{account.institution ?? "Не указана"}</strong></div><div><span>Тип</span><strong>{accountTypeLabels[account.account_type]}</strong></div><div><span>Валюта</span><strong>{account.currency}</strong></div><div><span>Статус</span><strong>{account.is_archived ? "В архиве" : "Активен"}</strong></div><div className="account-detail-secondary-actions">{account.is_archived ? <button className="text-button" onClick={() => void setArchived(false)} type="button">Восстановить</button> : <button className="text-button" onClick={() => void setArchived(true)} type="button">Архивировать</button>}<button className="text-button text-button--danger" onClick={() => void remove()} type="button">Удалить</button></div></section>

    <section className="panel account-detail-transactions">
      <div className="panel-heading"><div><span className="kicker">Активность</span><h2>Операции счёта</h2></div><button className="secondary-button" disabled={isLoading} onClick={() => void load()} type="button">Обновить</button></div>
      <form className="transaction-filters" onSubmit={(event) => { event.preventDefault(); setOffset(0); setAppliedSearch(search); }}><label className="transaction-search"><input aria-label="Поиск по операциям счёта" onChange={(event) => setSearch(event.target.value)} placeholder="Контрагент или описание" value={search}/></label><button className="secondary-button" type="submit">Найти</button><select aria-label="Период" onChange={(event) => { setOffset(0); setPeriod(event.target.value as "month" | "all"); }} value={period}><option value="month">Текущий месяц</option><option value="all">Всё время</option></select><select aria-label="Тип операции" onChange={(event) => { setOffset(0); setTypeFilter(event.target.value as "" | TransactionType); }} value={typeFilter}><option value="">Все типы</option><option value="expense">Расходы</option><option value="income">Доходы</option><option value="transfer">Переводы</option><option value="refund">Возвраты</option><option value="adjustment">Корректировки</option></select><select aria-label="Статус операции" onChange={(event) => { setOffset(0); setStatusFilter(event.target.value as "" | TransactionStatus); }} value={statusFilter}><option value="">Все статусы</option><option value="draft">Черновик</option><option value="confirmed">Подтверждена</option><option value="reconciled">Сверена</option><option value="cancelled">Отменена</option></select></form>
      {isLoading ? <div aria-label="Загружаем операции счёта" className="transaction-skeleton">{Array.from({ length: 4 }, (_, index) => <i key={index}/>)}</div> : transactions.length ? <AccountTransactionList accountId={account.id} timezone={timezone} transactions={transactions}/> : <div className="empty-state"><strong>Операций не найдено</strong><span>Измените фильтры или добавьте операцию для этого счёта.</span></div>}
      <div className="pagination"><span>{total ? `${offset + 1}–${Math.min(offset + transactionLimit, total)} из ${total}` : "0 операций"}</span><div><button className="secondary-button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - transactionLimit))} type="button">Назад</button><button className="secondary-button" disabled={offset + transactionLimit >= total} onClick={() => setOffset(offset + transactionLimit)} type="button">Дальше</button></div></div>
    </section>

    <section className="panel account-reconciliation-history">
      <div className="panel-heading"><div><span className="kicker">Контроль</span><h2>История сверок</h2></div><button className="secondary-button" onClick={() => setReconciliationOpen(true)} type="button">Новая сверка</button></div>
      {reconciliations.length ? <div className="account-reconciliation-history-list">{reconciliations.map((item) => <article key={item.id}><div><strong>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "long", timeZone: timezone }).format(new Date(`${item.statement_date}T12:00:00Z`))}</strong><span>Подтверждена {new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short", timeZone: timezone }).format(new Date(item.confirmed_at))}</span></div><div><strong>{formatMoney(item.statement_balance, item.currency)}</strong><span>{item.transaction_ids.length} операций</span></div><span className="status-chip status-chip--reconciled">Сверено</span></article>)}</div> : <div className="empty-state account-reconciliation-empty"><strong>Сверок пока нет</strong><span>Сравните баланс Finspace с банковской выпиской на выбранную дату.</span></div>}
    </section>

    {drawerOpen ? <AccountDrawer editing={account} form={form} isSaving={isSaving} onChange={setForm} onClose={() => setDrawerOpen(false)} onSave={(event) => void save(event)}/> : null}
    {reconciliationOpen ? <AccountReconciliationDialog account={account} onClose={() => setReconciliationOpen(false)} onConfirmed={async () => { await load(); }} onError={onError} timezone={timezone}/> : null}
  </section>;
}
