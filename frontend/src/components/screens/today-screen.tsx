"use client";

import { useCallback, useEffect, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type { AccountBalance, FinancialSummaryGroup } from "@/types/finance";

interface TodayScreenProps {
  onError: (error: unknown) => void;
}

export function TodayScreen({ onError }: TodayScreenProps) {
  const [balances, setBalances] = useState<AccountBalance[]>([]);
  const [summary, setSummary] = useState<FinancialSummaryGroup[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const [balanceResult, summaryResult] = await Promise.all([
        apiClient.get<AccountBalance[]>("/api/v1/accounts/balances"),
        apiClient.get<{ groups: FinancialSummaryGroup[] }>("/api/v1/financial-summary"),
      ]);
      setBalances(balanceResult);
      setSummary(summaryResult.groups);
    } catch (error) {
      onError(error);
    } finally {
      setIsLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Обзор</span>
          <h1>Сегодня</h1>
          <p>Остатки и денежный поток по валютам.</p>
        </div>
        <button className="secondary-button" type="button" onClick={() => void load()}>
          Обновить
        </button>
      </header>

      {isLoading ? <div className="empty-state">Считаем сводку…</div> : null}
      {!isLoading ? (
        <>
          <div className="metric-grid">
            {summary.length ? (
              summary.map((group) => (
                <article className="metric-card" key={group.currency}>
                  <span>{group.currency} · денежный поток</span>
                  <strong className={`money money--${moneyTone(group.net_cashflow)}`}>
                    {formatMoney(group.net_cashflow, group.currency)}
                  </strong>
                  <div className="metric-pair">
                    <small>Доход {formatMoney(group.income, group.currency)}</small>
                    <small>Расход {formatMoney(group.expense, group.currency)}</small>
                  </div>
                </article>
              ))
            ) : (
              <article className="metric-card metric-card--quiet">
                <span>Денежный поток</span>
                <strong>Пока нет операций</strong>
                <small>Добавьте доход или расход в разделе «Операции».</small>
              </article>
            )}
          </div>

          <div className="panel">
            <div className="panel-heading">
              <div>
                <span className="kicker">Счета</span>
                <h2>Текущие остатки</h2>
              </div>
            </div>
            <div className="balance-list">
              {balances.map((balance) => (
                <div className="balance-row" key={balance.account_id}>
                  <span>{balance.name}</span>
                  <strong className={`money money--${moneyTone(balance.balance)}`}>
                    {formatMoney(balance.balance, balance.currency)}
                  </strong>
                </div>
              ))}
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}
