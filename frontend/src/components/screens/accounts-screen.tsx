"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type { Account, AccountBalance, AccountType, Currency, Paged } from "@/types/finance";

interface AccountsScreenProps {
  onError: (error: unknown) => void;
}

interface AccountForm {
  name: string;
  accountType: AccountType;
  currency: Currency;
  institution: string;
  openingBalance: string;
  description: string;
}

const emptyForm: AccountForm = {
  name: "",
  accountType: "debit_card",
  currency: "RUB",
  institution: "",
  openingBalance: "0.0000",
  description: "",
};

const typeLabels: Record<AccountType, string> = {
  cash: "Наличные",
  debit_card: "Дебетовая карта",
  credit_card: "Кредитная карта",
  current_account: "Расчётный счёт",
  savings: "Накопительный",
  deposit: "Вклад",
  brokerage: "Брокерский",
  crypto_wallet: "Криптокошелёк",
  other: "Другой",
};

export function AccountsScreen({ onError }: AccountsScreenProps) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [balances, setBalances] = useState<AccountBalance[]>([]);
  const [form, setForm] = useState<AccountForm>(emptyForm);
  const [editing, setEditing] = useState<Account | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  const balanceById = useMemo(
    () => new Map(balances.map((balance) => [balance.account_id, balance])),
    [balances],
  );

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const [accountResult, balanceResult] = await Promise.all([
        apiClient.get<Paged<Account>>(`/api/v1/accounts?is_archived=${showArchived}&limit=200`),
        apiClient.get<AccountBalance[]>("/api/v1/accounts/balances"),
      ]);
      setAccounts(accountResult.items);
      setBalances(balanceResult);
    } catch (error) {
      onError(error);
    } finally {
      setIsLoading(false);
    }
  }, [onError, showArchived]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  function startEdit(account: Account) {
    setEditing(account);
    setForm({
      name: account.name,
      accountType: account.account_type,
      currency: account.currency,
      institution: account.institution ?? "",
      openingBalance: account.opening_balance,
      description: account.description ?? "",
    });
  }

  function resetForm() {
    setEditing(null);
    setForm(emptyForm);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setIsSaving(true);
    try {
      if (editing) {
        await apiClient.patch<Account>(`/api/v1/accounts/${editing.id}`, {
          version: editing.version,
          name: form.name,
          account_type: form.accountType,
          currency: form.currency,
          institution: form.institution || null,
          opening_balance: form.openingBalance,
          description: form.description || null,
        });
      } else {
        await apiClient.post<Account>("/api/v1/accounts", {
          name: form.name,
          account_type: form.accountType,
          currency: form.currency,
          institution: form.institution || null,
          opening_balance: form.openingBalance,
          opening_balance_at: new Date().toISOString(),
          description: form.description || null,
        });
      }
      resetForm();
      await load();
    } catch (error) {
      onError(error);
    } finally {
      setIsSaving(false);
    }
  }

  async function archive(account: Account) {
    try {
      await apiClient.delete<Account>(`/api/v1/accounts/${account.id}?version=${account.version}`);
      await load();
    } catch (error) {
      onError(error);
    }
  }

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Структура денег</span>
          <h1>Счета</h1>
          <p>Стартовый и рассчитанный остаток хранятся отдельно.</p>
        </div>
        <label className="toggle">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(event) => setShowArchived(event.target.checked)}
          />
          Архив
        </label>
      </header>

      <div className="two-column">
        <div className="panel">
          <div className="panel-heading">
            <h2>{showArchived ? "Архивные счета" : "Активные счета"}</h2>
            <button className="secondary-button" type="button" onClick={() => void load()}>
              Обновить
            </button>
          </div>
          {isLoading ? <div className="empty-state">Загружаем счета…</div> : null}
          {!isLoading && !accounts.length ? (
            <div className="empty-state">В этом разделе пока нет счетов.</div>
          ) : null}
          <div className="card-list">
            {accounts.map((account) => {
              const balance = balanceById.get(account.id);
              return (
                <article className="account-card" key={account.id}>
                  <div className="account-icon">
                    {account.account_type === "cash" ? "💵"
                      : account.account_type === "credit_card" ? "💳"
                      : account.account_type === "savings" ? "🏦"
                      : account.account_type === "deposit" ? "📈"
                      : account.account_type === "brokerage" ? "📊"
                      : account.account_type === "crypto_wallet" ? "🪙"
                      : "🏧"}
                  </div>
                  <div className="account-details">
                    <strong>{account.name}</strong>
                    <span>
                      {typeLabels[account.account_type]}
                      {account.institution ? ` · ${account.institution}` : ""}
                    </span>
                  </div>
                  <div className="account-total">
                    <strong className={`money money--${moneyTone(balance?.balance ?? "0")}`}>
                      {formatMoney(balance?.balance ?? account.opening_balance, account.currency)}
                    </strong>
                    <span>старт {formatMoney(account.opening_balance, account.currency)}</span>
                  </div>
                  {!account.is_archived ? (
                    <div className="row-actions">
                      <button className="text-button" type="button" onClick={() => startEdit(account)}>
                        Изменить
                      </button>
                      <button className="text-button text-button--danger" type="button" onClick={() => void archive(account)}>
                        В архив
                      </button>
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        </div>

        <form className="form-panel" onSubmit={(event) => void save(event)}>
          <div className="panel-heading">
            <div>
              <span className="kicker">{editing ? "Редактирование" : "Новый объект"}</span>
              <h2>{editing ? editing.name : "Добавить счёт"}</h2>
            </div>
            {editing ? (
              <button className="text-button" type="button" onClick={resetForm}>
                Отмена
              </button>
            ) : null}
          </div>
          <label>
            Название
            <input required maxLength={200} value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
          </label>
          <div className="form-grid">
            <label>
              Тип
              <select value={form.accountType} onChange={(event) => setForm({ ...form, accountType: event.target.value as AccountType })}>
                {Object.entries(typeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label>
              Валюта
              <select value={form.currency} onChange={(event) => setForm({ ...form, currency: event.target.value as Currency })}>
                <option>RUB</option><option>EUR</option><option>USD</option>
              </select>
            </label>
          </div>
          <label>
            Организация
            <input value={form.institution} onChange={(event) => setForm({ ...form, institution: event.target.value })} />
          </label>
          <label>
            Начальный остаток
            <input required inputMode="decimal" pattern="-?\d+(\.\d{1,4})?" value={form.openingBalance} onChange={(event) => setForm({ ...form, openingBalance: event.target.value })} />
          </label>
          <label>
            Описание
            <textarea rows={3} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} />
          </label>
          <button type="submit" disabled={isSaving}>{isSaving ? "Сохраняем…" : editing ? "Сохранить изменения" : "Создать счёт"}</button>
        </form>
      </div>
    </section>
  );
}
