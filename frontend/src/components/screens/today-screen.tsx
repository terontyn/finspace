"use client";

import { useCallback, useEffect, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type { AccountBalance, FinancialSummaryGroup } from "@/types/finance";

interface TodayScreenProps {
  onError: (error: unknown) => void;
}

function SkeletonMetricCard() {
  return (
    <div className="metric-card" style={{ animationDelay: "0ms" }}>
      <div className="skeleton skeleton-text" style={{ width: "60%", marginBottom: 20 }} />
      <div className="skeleton skeleton-title" style={{ width: "80%" }} />
      <div style={{ display: "flex", gap: 12, marginTop: 14 }}>
        <div className="skeleton skeleton-text" style={{ width: "40%" }} />
        <div className="skeleton skeleton-text" style={{ width: "40%" }} />
      </div>
    </div>
  );
}

function SkeletonBalanceRow() {
  return (
    <div className="balance-row" style={{ pointerEvents: "none" }}>
      <div className="skeleton skeleton-text" style={{ width: 120 }} />
      <div className="skeleton skeleton-text" style={{ width: 80 }} />
    </div>
  );
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

  const now = new Date();
  const dateLabel = now.toLocaleDateString("ru-RU", {
    weekday: "long",
    day: "numeric",
    month: "long",
  });

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Обзор · {dateLabel}</span>
          <h1>Сегодня</h1>
          <p>Остатки и денежный поток по всем счетам.</p>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void load()}
          disabled={isLoading}
        >
          {isLoading ? (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ animation: "spin 0.7s linear infinite" }}>
              <path d="M21 12a9 9 0 1 1-6.219-8.56" />
            </svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.18"/>
            </svg>
          )}
          Обновить
        </button>
      </header>

      {/* Метрики cashflow */}
      <div className="metric-grid">
        {isLoading ? (
          <>
            <SkeletonMetricCard />
            <SkeletonMetricCard />
            <SkeletonMetricCard />
          </>
        ) : summary.length ? (
          summary.map((group, i) => (
            <article
              className={`metric-card metric-card--${moneyTone(group.net_cashflow)}`}
              key={group.currency}
              style={{ animationDelay: `${i * 60}ms` }}
            >
              <div className="metric-label">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>
                </svg>
                {group.currency} · Денежный поток
              </div>
              <strong className={`metric-value money money--${moneyTone(group.net_cashflow)}`}>
                {formatMoney(group.net_cashflow, group.currency)}
              </strong>
              <div className="metric-pair">
                <small style={{ color: "var(--positive)" }}>
                  ↑ {formatMoney(group.income, group.currency)}
                </small>
                <small style={{ color: "var(--negative)" }}>
                  ↓ {formatMoney(group.expense, group.currency)}
                </small>
              </div>
            </article>
          ))
        ) : (
          <article className="metric-card metric-card--quiet metric-card--neutral">
            <div className="metric-label">Денежный поток</div>
            <strong className="metric-value" style={{ color: "var(--text-secondary)" }}>
              Нет данных
            </strong>
            <small>Добавьте доход или расход в разделе «Операции».</small>
          </article>
        )}
      </div>

      {/* Остатки по счетам */}
      <div className="panel">
        <div className="panel-heading">
          <div>
            <span className="kicker">Счета</span>
            <h2>Текущие остатки</h2>
          </div>
        </div>
        <div className="balance-list">
          {isLoading ? (
            <>
              <SkeletonBalanceRow />
              <SkeletonBalanceRow />
              <SkeletonBalanceRow />
            </>
          ) : balances.length ? (
            balances.map((balance) => (
              <div className="balance-row" key={balance.account_id}>
                <span className="balance-name">{balance.name}</span>
                <strong className={`balance-amount money money--${moneyTone(balance.balance)}`}>
                  {formatMoney(balance.balance, balance.currency)}
                </strong>
              </div>
            ))
          ) : (
            <div className="empty-state">
              <span className="empty-state-icon">🏦</span>
              <span>Нет активных счетов.</span>
              <span>Создайте счёт в разделе «Счета».</span>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
