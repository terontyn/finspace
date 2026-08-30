import type { Currency, Transaction } from "@/types/finance";

export interface TransactionSplitForm {
  amount: string;
  categoryId: string;
  comment: string;
}

export interface TransactionForm {
  accountId: string;
  amount: string;
  categoryId: string;
  comment: string;
  counterparty: string;
  currency: Currency;
  description: string;
  occurredAt: string;
  payeeId: string;
  splits: TransactionSplitForm[];
  status: "draft" | "confirmed";
  targetAccountId: string;
  transactionType: "income" | "expense" | "transfer";
}

function localDateTime(value: Date): string {
  const local = new Date(value);
  local.setMinutes(local.getMinutes() - local.getTimezoneOffset());
  return local.toISOString().slice(0, 16);
}

export function initialTransactionForm(now = new Date()): TransactionForm {
  return {
    accountId: "", amount: "", categoryId: "", comment: "", counterparty: "", currency: "RUB",
    description: "", occurredAt: localDateTime(now), payeeId: "", splits: [], status: "confirmed", targetAccountId: "", transactionType: "expense",
  };
}

export function transactionFormFromRecord(transaction: Transaction): TransactionForm | null {
  if (!(["income", "expense", "transfer"] as const).includes(transaction.transaction_type as "income" | "expense" | "transfer")) return null;
  return {
    accountId: transaction.account.id,
    amount: transaction.amount,
    categoryId: transaction.category?.id ?? "",
    comment: transaction.comment ?? "",
    counterparty: transaction.counterparty ?? "",
    currency: transaction.currency,
    description: transaction.description ?? "",
    occurredAt: localDateTime(new Date(transaction.occurred_at)),
    payeeId: transaction.payee?.id ?? "",
    splits: transaction.splits.map((split) => ({ amount: split.amount, categoryId: split.category_id, comment: "" })),
    status: transaction.status === "draft" ? "draft" : "confirmed",
    targetAccountId: transaction.target_account?.id ?? "",
    transactionType: transaction.transaction_type as TransactionForm["transactionType"],
  };
}

export function transactionPayload(form: TransactionForm) {
  const splits = form.transactionType === "transfer" ? [] : form.splits
    .filter((split) => split.categoryId && split.amount)
    .map((split) => ({ category_id: split.categoryId, amount: split.amount, comment: split.comment || null }));
  return {
    occurred_at: new Date(form.occurredAt).toISOString(),
    transaction_type: form.transactionType,
    amount: form.amount,
    currency: form.currency,
    account_id: form.accountId,
    target_account_id: form.transactionType === "transfer" ? form.targetAccountId : null,
    category_id: form.transactionType === "transfer" || splits.length ? null : form.categoryId || null,
    payee_id: form.payeeId || null,
    counterparty: form.counterparty || null,
    description: form.description || null,
    comment: form.comment || null,
    status: form.status,
    splits,
  };
}

export function transactionFormWithPayee(form: TransactionForm, payeeId: string): TransactionForm {
  return { ...form, payeeId };
}

export function transactionFormWithCounterparty(form: TransactionForm, counterparty: string): TransactionForm {
  return { ...form, counterparty };
}

export function transactionMutation(form: TransactionForm, editing: Transaction | null) {
  const payload = transactionPayload(form);
  return editing
    ? { method: "PATCH" as const, path: `/api/v1/transactions/${editing.id}`, body: { ...payload, version: editing.version } }
    : { method: "POST" as const, path: "/api/v1/transactions", body: payload };
}

export function transactionCancelMutation(transaction: Transaction) {
  return { path: `/api/v1/transactions/${transaction.id}/cancel`, body: { version: transaction.version } };
}
