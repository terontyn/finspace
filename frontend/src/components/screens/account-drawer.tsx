"use client";

import { EntityDrawer } from "@/components/ui/entity-drawer";
import type { Account, AccountType, Currency } from "@/types/finance";

import type { AccountForm } from "./account-form";

export const accountTypeLabels: Record<AccountType, string> = {
  brokerage: "Брокерский",
  cash: "Наличные",
  credit_card: "Кредитная карта",
  crypto_wallet: "Криптокошелёк",
  current_account: "Расчётный счёт",
  debit_card: "Дебетовая карта",
  deposit: "Вклад",
  other: "Другой",
  savings: "Накопительный",
};

export const accountTypeSymbols: Record<AccountType, string> = {
  brokerage: "Б",
  cash: "₽",
  credit_card: "К",
  crypto_wallet: "₿",
  current_account: "Р",
  debit_card: "Д",
  deposit: "В",
  other: "С",
  savings: "Н",
};

export function AccountDrawer({ editing, form, isSaving, onChange, onClose, onSave }: {
  editing: Account | null;
  form: AccountForm;
  isSaving: boolean;
  onChange: (form: AccountForm) => void;
  onClose: () => void;
  onSave: (event: React.FormEvent) => void;
}) {
  return <EntityDrawer ariaLabel={editing ? "Редактирование счёта" : "Новый счёт"} eyebrow={editing ? "Редактирование" : "Новый объект"} onClose={onClose} subtitle={editing ? `Версия ${editing.version}` : "Баланс будет рассчитываться backend"} title={editing ? editing.name : "Добавить счёт"}>
    <form className="entity-form" onSubmit={onSave}>
      <label><span>Название</span><input maxLength={200} onChange={(event) => onChange({ ...form, name: event.target.value })} required value={form.name}/></label>
      <div className="entity-form-grid"><label><span>Тип</span><select onChange={(event) => { const accountType = event.target.value as AccountType; onChange({ ...form, accountType, creditLimit: accountType === "credit_card" ? form.creditLimit : "" }); }} value={form.accountType}>{Object.entries(accountTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label><span>Валюта</span><select onChange={(event) => onChange({ ...form, currency: event.target.value as Currency })} value={form.currency}><option value="RUB">RUB</option><option value="EUR">EUR</option><option value="USD">USD</option></select></label></div>
      <label><span>Организация</span><input maxLength={200} onChange={(event) => onChange({ ...form, institution: event.target.value })} placeholder="Необязательно" value={form.institution}/></label>
      <div className="entity-form-grid"><label><span>Начальный остаток</span><input inputMode="decimal" onChange={(event) => onChange({ ...form, openingBalance: event.target.value })} pattern="-?\d+(\.\d{1,4})?" required value={form.openingBalance}/></label><label><span>Дата начального остатка</span><input onChange={(event) => onChange({ ...form, openingBalanceAt: event.target.value })} required type="datetime-local" value={form.openingBalanceAt}/></label></div>
      {form.accountType === "credit_card" ? <label><span>Кредитный лимит</span><input inputMode="decimal" onChange={(event) => onChange({ ...form, creditLimit: event.target.value })} pattern="-?\d+(\.\d{1,4})?" placeholder="Необязательно" value={form.creditLimit}/></label> : null}
      <label><span>Описание</span><textarea onChange={(event) => onChange({ ...form, description: event.target.value })} placeholder="Назначение счёта" rows={3} value={form.description}/></label>
      <footer><button className="secondary-button" onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={isSaving} type="submit">{isSaving ? "Сохраняем…" : editing ? "Сохранить изменения" : "Создать счёт"}</button></footer>
    </form>
  </EntityDrawer>;
}
