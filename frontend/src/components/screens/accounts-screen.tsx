"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type { Account, AccountBalance, Paged } from "@/types/finance";

import { AccountDrawer, accountTypeLabels, accountTypeSymbols } from "./account-drawer";
import { accountArchiveMutation, accountDeleteMutation, accountFormFromRecord, accountMutation, accountRestoreDeletedMutation, initialAccountForm, type AccountForm } from "./account-form";
import { groupBalanceTotals } from "./dashboard-data";

interface AccountsScreenProps { onError: (error: unknown) => void; }

function AccountRow({ account, balance, onArchive, onDelete, onEdit, onRestore }: {
  account: Account;
  balance?: AccountBalance;
  onArchive: (account: Account) => void;
  onDelete: (account: Account) => void;
  onEdit: (account: Account) => void;
  onRestore: (account: Account) => void;
}) {
  const currentBalance = balance?.balance ?? account.opening_balance;
  return <article className="account-list-row" data-account-id={account.id}>
    <Link aria-label={`Открыть счёт ${account.name}`} className="account-type-symbol" href={`/accounts/${account.id}`}>{accountTypeSymbols[account.account_type]}</Link>
    <div className="account-list-primary"><Link href={`/accounts/${account.id}`}><strong>{account.name}</strong></Link><span>{accountTypeLabels[account.account_type]}{account.institution ? ` · ${account.institution}` : ""}</span></div>
    <div className="account-list-meta"><span>Начальный остаток</span><strong>{formatMoney(account.opening_balance, account.currency)}</strong></div>
    {account.credit_limit ? <div className="account-list-meta"><span>Кредитный лимит</span><strong>{formatMoney(account.credit_limit, account.currency)}</strong></div> : <div className="account-list-meta"><span>Дата отсчёта</span><strong>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium" }).format(new Date(account.opening_balance_at))}</strong></div>}
    <div className="account-list-balance"><span>Текущий остаток</span><strong className={`money--${moneyTone(currentBalance)}`}>{formatMoney(currentBalance, account.currency)}</strong></div>
    <div className="account-list-actions">{account.is_archived ? <button className="text-button" onClick={() => onRestore(account)} type="button">Восстановить</button> : <><button className="text-button" onClick={() => onEdit(account)} type="button">Изменить</button><button className="text-button" onClick={() => onArchive(account)} type="button">В архив</button></>}<button className="text-button text-button--danger" onClick={() => onDelete(account)} type="button">Удалить</button></div>
  </article>;
}

export function AccountsScreen({ onError }: AccountsScreenProps) {
  const [activeAccounts, setActiveAccounts] = useState<Account[]>([]);
  const [archivedAccounts, setArchivedAccounts] = useState<Account[]>([]);
  const [balances, setBalances] = useState<AccountBalance[]>([]);
  const [form, setForm] = useState<AccountForm>(() => initialAccountForm());
  const [editing, setEditing] = useState<Account | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [deletedAccount, setDeletedAccount] = useState<Account | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  const balanceById = useMemo(() => new Map(balances.map((balance) => [balance.account_id, balance])), [balances]);
  const activeIds = useMemo(() => new Set(activeAccounts.map((account) => account.id)), [activeAccounts]);
  const activeTotals = useMemo(() => groupBalanceTotals(balances.filter((balance) => activeIds.has(balance.account_id))), [activeIds, balances]);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const [activeResult, archivedResult, balanceResult] = await Promise.all([
        apiClient.get<Paged<Account>>("/api/v1/accounts?is_archived=false&limit=200"),
        apiClient.get<Paged<Account>>("/api/v1/accounts?is_archived=true&limit=200"),
        apiClient.get<AccountBalance[]>("/api/v1/accounts/balances"),
      ]);
      setActiveAccounts(activeResult.items);
      setArchivedAccounts(archivedResult.items);
      setBalances(balanceResult);
    } catch (error) { onError(error); } finally { setIsLoading(false); }
  }, [onError]);

  useEffect(() => { queueMicrotask(() => void load()); }, [load]);

  function openCreate() { setEditing(null); setForm(initialAccountForm()); setDrawerOpen(true); }
  function startEdit(account: Account) { setEditing(account); setForm(accountFormFromRecord(account)); setDrawerOpen(true); }
  function closeDrawer() { setDrawerOpen(false); setEditing(null); setForm(initialAccountForm()); }

  async function save(event: React.FormEvent) {
    event.preventDefault(); setIsSaving(true);
    const mutation = accountMutation(form, editing);
    try {
      if (mutation.method === "PATCH") await apiClient.patch<Account>(mutation.path, mutation.body);
      else await apiClient.post<Account>(mutation.path, mutation.body);
      closeDrawer(); await load();
    } catch (error) { onError(error); } finally { setIsSaving(false); }
  }

  async function setArchived(account: Account, isArchived: boolean) {
    const mutation = accountArchiveMutation(account, isArchived);
    try { await apiClient.patch<Account>(mutation.path, mutation.body); await load(); } catch (error) { onError(error); }
  }

  async function remove(account: Account) {
    if (!window.confirm(`Удалить счёт «${account.name}»? Его можно восстановить до перезагрузки страницы.`)) return;
    const mutation = accountDeleteMutation(account);
    try { const deleted = await apiClient.delete<Account>(mutation.path); setDeletedAccount(deleted); await load(); } catch (error) { onError(error); }
  }

  async function restoreDeleted() {
    if (!deletedAccount) return;
    const mutation = accountRestoreDeletedMutation(deletedAccount);
    try { await apiClient.post<Account>(mutation.path, mutation.body); setDeletedAccount(null); await load(); } catch (error) { onError(error); }
  }

  return <section>
    <header className="screen-header"><div><span className="kicker">Структура денег</span><h1>Счета</h1><p>Текущие остатки рассчитываются backend из начального баланса и подтверждённых операций.</p></div><div className="screen-header-actions"><button className="secondary-button" disabled={isLoading} onClick={() => void load()} type="button">Обновить</button><button className="primary-button" onClick={openCreate} type="button">＋ Новый счёт</button></div></header>

    {deletedAccount ? <div className="undo-banner" role="status"><span>Счёт «{deletedAccount.name}» удалён.</span><button className="text-button" onClick={() => void restoreDeleted()} type="button">Восстановить</button><button aria-label="Скрыть уведомление" className="text-button" onClick={() => setDeletedAccount(null)} type="button">×</button></div> : null}

    <section aria-label="Сводка по активным счетам" className="account-summary-grid">
      <article><span>Активные счета</span><strong>{activeAccounts.length}</strong><small>Архивных: {archivedAccounts.length}</small></article>
      {activeTotals.map((total) => <article key={total.currency}><span>Баланс · {total.currency}</span><strong className={`money--${moneyTone(total.total)}`}>{formatMoney(total.total, total.currency)}</strong><small>{total.accountsCount} активных счетов</small></article>)}
    </section>

    <section className="panel accounts-section">
      <div className="panel-heading"><div><span className="kicker">Рабочие счета</span><h2>Активные</h2></div><span className="count-badge">{activeAccounts.length}</span></div>
      {isLoading ? <div className="account-list-skeleton">{Array.from({ length: 3 }, (_, index) => <i key={index}/>)}</div> : activeAccounts.length ? <div className="account-compact-list">{activeAccounts.map((account) => <AccountRow account={account} balance={balanceById.get(account.id)} key={account.id} onArchive={(value) => void setArchived(value, true)} onDelete={(value) => void remove(value)} onEdit={startEdit} onRestore={(value) => void setArchived(value, false)}/>)}</div> : <div className="empty-state"><strong>Активных счетов нет</strong><span>Создайте счёт или восстановите его из архива.</span></div>}
    </section>

    <section className="panel accounts-section accounts-section--archived">
      <div className="panel-heading"><div><span className="kicker">Не участвуют в работе</span><h2>Архив</h2></div><span className="count-badge">{archivedAccounts.length}</span></div>
      {!isLoading && archivedAccounts.length ? <div className="account-compact-list">{archivedAccounts.map((account) => <AccountRow account={account} balance={balanceById.get(account.id)} key={account.id} onArchive={(value) => void setArchived(value, true)} onDelete={(value) => void remove(value)} onEdit={startEdit} onRestore={(value) => void setArchived(value, false)}/>)}</div> : !isLoading ? <div className="empty-state"><strong>Архив пуст</strong><span>Здесь появятся счета, которые больше не используются.</span></div> : null}
    </section>

    {drawerOpen ? <AccountDrawer editing={editing} form={form} isSaving={isSaving} onChange={setForm} onClose={closeDrawer} onSave={(event) => void save(event)}/> : null}
  </section>;
}
