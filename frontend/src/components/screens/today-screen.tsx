"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type { AccountBalance, FinancialSummaryGroup, Paged, Transaction } from "@/types/finance";

import { currentMonthPeriod, financialSummaryUrl, groupBalanceTotals } from "./dashboard-data";
import { transactionPartyDetail, transactionPartyName } from "./transaction-presenters";

interface TodayScreenProps { onError: (error: unknown) => void; timezone: string; }

const transactionLabels: Record<Transaction["transaction_type"], string> = {
  adjustment: "Корректировка",
  expense: "Расход",
  income: "Доход",
  refund: "Возврат",
  transfer: "Перевод",
};

const transactionStatusLabels: Record<Transaction["status"], string> = {
  cancelled: "Отменена",
  confirmed: "Подтверждена",
  draft: "Черновик",
  reconciled: "Сверена",
};

function DashboardSkeleton() {
  return <div aria-label="Загружаем финансовый обзор" className="dashboard-skeleton">
    {Array.from({ length: 6 }, (_, index) => <i key={index}/>) }
  </div>;
}

export function TodayScreen({ onError, timezone }: TodayScreenProps) {
  const [balances, setBalances] = useState<AccountBalance[]>([]);
  const [summary, setSummary] = useState<FinancialSummaryGroup[]>([]);
  const [recent, setRecent] = useState<Transaction[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const period = useMemo(() => currentMonthPeriod(new Date(), timezone), [timezone]);
  const balanceTotals = useMemo(() => groupBalanceTotals(balances), [balances]);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const [balanceResult, summaryResult, transactionResult] = await Promise.all([
        apiClient.get<AccountBalance[]>("/api/v1/accounts/balances"),
        apiClient.get<{ groups: FinancialSummaryGroup[] }>(financialSummaryUrl(period)),
        apiClient.get<Paged<Transaction>>("/api/v1/transactions?limit=6&offset=0"),
      ]);
      setBalances(balanceResult);
      setSummary(summaryResult.groups);
      setRecent(transactionResult.items);
    } catch (error) {
      onError(error);
    } finally {
      setIsLoading(false);
    }
  }, [onError, period]);

  useEffect(() => { queueMicrotask(() => void load()); }, [load]);

  const dateLabel = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "long", timeZone: timezone, weekday: "long",
  }).format(new Date());

  return <section>
    <header className="screen-header dashboard-header">
      <div><span className="kicker">Обзор · {dateLabel}</span><h1>Сегодня</h1><p>Реальные остатки и денежный поток за {period.label}.</p></div>
      <div className="screen-header-actions"><button className="secondary-button" disabled={isLoading} onClick={() => void load()} type="button">Обновить</button><Link className="primary-button" href="/transactions?new=1">＋ Операция</Link></div>
    </header>

    {isLoading ? <DashboardSkeleton/> : <>
      <section aria-label="Текущие остатки" className="dashboard-balance-summary">
        {balanceTotals.length ? balanceTotals.map((total) => <article className="dashboard-total-card" key={total.currency}>
          <span>Текущий баланс · {total.currency}</span>
          <strong className={`money money--${moneyTone(total.total)}`}>{formatMoney(total.total, total.currency)}</strong>
          <small>{total.accountsCount} {total.accountsCount === 1 ? "счёт" : "счета"} · без конвертации валют</small>
        </article>) : <article className="dashboard-total-card dashboard-total-card--empty"><span>Текущий баланс</span><strong>Нет данных</strong><small>Создайте первый счёт, чтобы увидеть остаток.</small></article>}
      </section>

      <section aria-label={`Денежный поток за ${period.label}`} className="dashboard-cashflow-grid">
        {summary.length ? summary.map((group) => <article className="dashboard-cashflow-card panel" key={group.currency}>
          <div className="panel-heading"><div><span className="kicker">Денежный поток</span><h2>{group.currency} · {period.label}</h2></div><span className="count-badge">{group.transactions_count}</span></div>
          <div className="dashboard-cashflow-values">
            <div><span>Доходы</span><strong className="money--positive">{formatMoney(group.income, group.currency)}</strong></div>
            <div><span>Расходы</span><strong>{formatMoney(group.expense, group.currency)}</strong></div>
            <div><span>Чистый поток</span><strong className={`money--${moneyTone(group.net_cashflow)}`}>{formatMoney(group.net_cashflow, group.currency)}</strong></div>
          </div>
          <footer><span>Переводы: {formatMoney(group.transfer_volume, group.currency)}</span><span>Подтверждённые и сверенные операции</span></footer>
        </article>) : <article className="panel dashboard-cashflow-empty"><span className="kicker">Денежный поток</span><h2>За текущий месяц операций нет</h2><p>В расчёт входят подтверждённые и сверенные операции.</p></article>}
      </section>

      <div className="dashboard-detail-grid">
        <section className="panel dashboard-list-card">
          <div className="panel-heading"><div><span className="kicker">Счета</span><h2>Остатки</h2></div><Link className="text-button" href="/accounts">Все счета →</Link></div>
          {balances.length ? <div className="dashboard-account-list">{balances.map((balance) => <div className="dashboard-account-row" key={balance.account_id}><span className="account-symbol">{balance.name.slice(0, 1).toUpperCase()}</span><div><strong>{balance.name}</strong><small>{balance.currency} · старт {formatMoney(balance.opening_balance, balance.currency)}</small></div><b className={`money--${moneyTone(balance.balance)}`}>{formatMoney(balance.balance, balance.currency)}</b></div>)}</div> : <div className="empty-state"><strong>Активных счетов пока нет</strong><span>Добавьте счёт в соответствующем разделе.</span></div>}
        </section>

        <section className="panel dashboard-list-card">
          <div className="panel-heading"><div><span className="kicker">Последнее</span><h2>Недавние операции</h2></div><Link className="text-button" href="/transactions">Все операции →</Link></div>
          {recent.length ? <div className="dashboard-recent-list">{recent.map((transaction) => <Link className="dashboard-recent-row" href="/transactions" key={transaction.id}><span className={`transaction-type-icon transaction-type-icon--${transaction.transaction_type}`}>{transaction.transaction_type === "income" ? "↓" : transaction.transaction_type === "transfer" ? "↔" : "↑"}</span><div><strong>{transactionPartyName(transaction)}</strong><small>{transactionPartyDetail(transaction) ? `${transactionPartyDetail(transaction)} · ` : ""}{transaction.category?.name ?? transactionLabels[transaction.transaction_type]} · {new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", timeZone: timezone }).format(new Date(transaction.occurred_at))} · {transactionStatusLabels[transaction.status]}</small></div><b className={`amount-cell--${transaction.transaction_type}`}>{transaction.transaction_type === "expense" ? "− " : transaction.transaction_type === "income" ? "+ " : ""}{formatMoney(transaction.amount, transaction.currency)}</b></Link>)}</div> : <div className="empty-state"><strong>Операций пока нет</strong><span>Добавьте первую запись в финансовый журнал.</span></div>}
        </section>
      </div>
    </>}
  </section>;
}
