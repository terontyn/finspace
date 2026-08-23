import { formatMoney } from "@/lib/money";
import type { Transaction } from "@/types/finance";

export const transactionTypeLabels: Record<Transaction["transaction_type"], string> = {
  adjustment: "Корректировка",
  expense: "Расход",
  income: "Доход",
  refund: "Возврат",
  transfer: "Перевод",
};

export const transactionStatusLabels: Record<Transaction["status"], string> = {
  cancelled: "Отменена",
  confirmed: "Подтверждена",
  draft: "Черновик",
  reconciled: "Сверена",
};

export const transactionSourceLabels: Record<Transaction["source"], string> = {
  api: "API",
  automation: "Автоматизация",
  google_sheets: "Google Sheets",
  import: "Импорт",
  manual: "Вручную",
  system: "Система",
  telegram: "Telegram",
};

export function TransactionStatusChip({ transaction }: { transaction: Transaction }) {
  return <span className={`status-chip status-chip--${transaction.status}`}>{transactionStatusLabels[transaction.status]}</span>;
}

export function TransactionAmount({ accountId, transaction }: { accountId?: string; transaction: Transaction }) {
  const incomingTransfer = transaction.transaction_type === "transfer" && transaction.target_account?.id === accountId;
  const outgoingTransfer = transaction.transaction_type === "transfer" && transaction.account.id === accountId;
  const positive = transaction.transaction_type === "income" || incomingTransfer || transaction.transaction_type === "adjustment";
  const negative = transaction.transaction_type === "expense" || outgoingTransfer;
  return <span className={`amount-cell--${positive ? "income" : negative ? "expense" : transaction.transaction_type}`}>
    {positive ? "+ " : negative ? "− " : ""}{formatMoney(transaction.amount, transaction.currency)}
  </span>;
}

export function transactionIcon(transaction: Transaction): string {
  if (transaction.transaction_type === "income") return "↓";
  if (transaction.transaction_type === "transfer") return "↔";
  return "↑";
}
