import type { Transaction } from "@/types/finance";

import { TransactionAmount, TransactionStatusChip, transactionIcon, transactionSourceLabels, transactionTypeLabels } from "./transaction-presenters";

export function AccountTransactionList({ accountId, timezone, transactions }: {
  accountId: string;
  timezone: string;
  transactions: Transaction[];
}) {
  const dateFormatter = new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "short",
    timeZone: timezone,
  });

  return <>
    <div className="transaction-table-wrap account-transaction-table"><table className="transaction-table"><thead><tr><th>Дата</th><th>Контрагент</th><th>Категория</th><th>Описание</th><th>Статус</th><th>Источник</th><th>Сумма</th></tr></thead><tbody>{transactions.map((transaction) => <tr key={transaction.id}><td><time dateTime={transaction.occurred_at}>{dateFormatter.format(new Date(transaction.occurred_at))}</time></td><td><strong>{transaction.counterparty ?? transactionTypeLabels[transaction.transaction_type]}</strong></td><td><span className="category-pill">{transaction.splits.length ? `${transaction.splits.length} категории` : transaction.category?.name ?? "Без категории"}</span></td><td>{transaction.description ?? "—"}</td><td><TransactionStatusChip transaction={transaction}/></td><td>{transactionSourceLabels[transaction.source]}</td><td className="amount-cell"><TransactionAmount accountId={accountId} transaction={transaction}/></td></tr>)}</tbody></table></div>
    <div className="transaction-mobile-list account-transaction-mobile-list">{transactions.map((transaction) => <article className="transaction-mobile-card" key={transaction.id}><div><span className={`transaction-type-icon transaction-type-icon--${transaction.transaction_type}`}>{transactionIcon(transaction)}</span><div><strong>{transaction.counterparty ?? transactionTypeLabels[transaction.transaction_type]}</strong><small>{transaction.category?.name ?? "Без категории"} · {dateFormatter.format(new Date(transaction.occurred_at))}</small></div><b><TransactionAmount accountId={accountId} transaction={transaction}/></b></div><footer><TransactionStatusChip transaction={transaction}/><span>{transactionSourceLabels[transaction.source]}</span></footer></article>)}</div>
  </>;
}
