import type { Account, AccountType, Currency } from "@/types/finance";

export interface AccountForm {
  accountType: AccountType;
  creditLimit: string;
  currency: Currency;
  description: string;
  institution: string;
  name: string;
  openingBalance: string;
  openingBalanceAt: string;
}

function localDateTime(value: Date): string {
  const local = new Date(value);
  local.setMinutes(local.getMinutes() - local.getTimezoneOffset());
  return local.toISOString().slice(0, 16);
}

export function initialAccountForm(now = new Date()): AccountForm {
  return {
    accountType: "debit_card",
    creditLimit: "",
    currency: "RUB",
    description: "",
    institution: "",
    name: "",
    openingBalance: "0.0000",
    openingBalanceAt: localDateTime(now),
  };
}

export function accountFormFromRecord(account: Account): AccountForm {
  return {
    accountType: account.account_type,
    creditLimit: account.credit_limit ?? "",
    currency: account.currency,
    description: account.description ?? "",
    institution: account.institution ?? "",
    name: account.name,
    openingBalance: account.opening_balance,
    openingBalanceAt: localDateTime(new Date(account.opening_balance_at)),
  };
}

export function accountPayload(form: AccountForm) {
  return {
    account_type: form.accountType,
    credit_limit: form.accountType === "credit_card" && form.creditLimit ? form.creditLimit : null,
    currency: form.currency,
    description: form.description || null,
    institution: form.institution || null,
    name: form.name,
    opening_balance: form.openingBalance,
    opening_balance_at: new Date(form.openingBalanceAt).toISOString(),
  };
}

export function accountMutation(form: AccountForm, editing: Account | null) {
  const payload = accountPayload(form);
  return editing
    ? { method: "PATCH" as const, path: `/api/v1/accounts/${editing.id}`, body: { ...payload, version: editing.version } }
    : { method: "POST" as const, path: "/api/v1/accounts", body: payload };
}

export function accountArchiveMutation(account: Account, isArchived: boolean) {
  return { path: `/api/v1/accounts/${account.id}`, body: { version: account.version, is_archived: isArchived } };
}

export function accountDeleteMutation(account: Account) {
  return { path: `/api/v1/accounts/${account.id}?version=${account.version}` };
}

export function accountRestoreDeletedMutation(account: Account) {
  return { path: `/api/v1/accounts/${account.id}/restore`, body: { version: account.version } };
}
