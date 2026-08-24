"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type { FinancialReport, FinancialReportGroup } from "@/types/finance";

import {
  currentReportMonth,
  financialReportUrl,
  parseReportQuery,
  type ReportCurrencyFilter,
} from "./reports-data";

interface ReportsScreenProps {
  onError: (error: unknown) => void;
  timezone: string;
}

const currencyOptions: Array<{ label: string; value: ReportCurrencyFilter }> = [
  { label: "Все валюты", value: "ALL" },
  { label: "RUB", value: "RUB" },
  { label: "USD", value: "USD" },
  { label: "EUR", value: "EUR" },
];

function monthLabel(month: string, timezone: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    month: "long",
    timeZone: timezone,
    year: "numeric",
  }).format(new Date(`${month}-15T12:00:00Z`));
}

function syncReportUrl(month: string, currency: ReportCurrencyFilter) {
  const url = new URL(window.location.href);
  url.searchParams.set("period", month);
  if (currency === "ALL") url.searchParams.delete("currency");
  else url.searchParams.set("currency", currency);
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

function ReportSkeleton() {
  return <div aria-label="Загружаем финансовые отчёты" className="reports-skeleton">
    {Array.from({ length: 7 }, (_, index) => <i key={index}/>) }
  </div>;
}

function ReportGroupView({ group, timezone }: { group: FinancialReportGroup; timezone: string }) {
  return <section aria-label={`Отчёт ${group.currency}`} className="report-currency-section">
    <div className="report-currency-heading"><div><span className="kicker">Валюта без конвертации</span><h2>{group.currency}</h2></div><span className="count-badge">{group.transactions_count} операций</span></div>

    <section aria-label={`Основные показатели ${group.currency}`} className="report-kpi-grid">
      <article><span>Доходы</span><strong className="money--positive">{formatMoney(group.income, group.currency)}</strong><small>С учётом возвратов доходов</small></article>
      <article><span>Расходы</span><strong className="money--negative">{formatMoney(group.expense, group.currency)}</strong><small>С учётом возвратов расходов</small></article>
      <article><span>Cash Flow</span><strong className={`money--${moneyTone(group.net_cashflow)}`}>{formatMoney(group.net_cashflow, group.currency)}</strong><small>Доходы − расходы + корректировки</small></article>
      <article><span>Переводы</span><strong>{formatMoney(group.transfer_volume, group.currency)}</strong><small>Не входят в доходы и расходы</small></article>
    </section>

    <div className="report-detail-grid">
      <section className="panel report-panel">
        <div className="panel-heading"><div><span className="kicker">Структура расходов</span><h3>По категориям</h3></div></div>
        {group.spending_by_category.length ? <div className="report-category-list">{group.spending_by_category.map((item) => <div className="report-category-row" key={item.category_id ?? "uncategorized"}><div><strong>{item.name}</strong><small>{item.transaction_count} движений с учётом возвратов</small></div><b className={`money--${moneyTone(item.amount)}`}>{formatMoney(item.amount, group.currency)}</b></div>)}</div> : <div className="empty-state report-empty"><strong>Расходов по категориям нет</strong><span>За выбранный месяц нет опубликованных expense-операций.</span></div>}
      </section>

      <section className="panel report-panel">
        <div className="panel-heading"><div><span className="kicker">Динамика</span><h3>Сравнение месяцев</h3></div></div>
        <div className="report-month-table" role="table" aria-label={`Сравнение месяцев ${group.currency}`}>
          <div className="report-month-row report-month-row--head" role="row"><span>Месяц</span><span>Доходы</span><span>Расходы</span><span>Cash Flow</span></div>
          {group.monthly_comparison.map((item) => <div className="report-month-row" role="row" key={item.month}><strong>{monthLabel(item.month, timezone)}</strong><span className="money--positive">{formatMoney(item.income, group.currency)}</span><span>{formatMoney(item.expense, group.currency)}</span><b className={`money--${moneyTone(item.net_cashflow)}`}>{formatMoney(item.net_cashflow, group.currency)}</b></div>)}
        </div>
      </section>
    </div>

    <section className="panel report-panel report-largest-panel">
      <div className="panel-heading"><div><span className="kicker">Транзакции</span><h3>Крупнейшие расходы</h3><p>Gross-суммы исходных расходов; возвраты уже учтены в KPI и категориях.</p></div><Link className="text-button" href="/transactions">Все операции →</Link></div>
      {group.largest_expenses.length ? <div className="report-expense-list">{group.largest_expenses.map((item, index) => <article key={item.transaction_id}><span className="report-rank">{String(index + 1).padStart(2, "0")}</span><div><strong>{item.counterparty ?? item.description ?? "Расход"}</strong><small>{item.category_name} · {item.account_name} · {new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: timezone }).format(new Date(item.occurred_at))}</small></div><b>{formatMoney(item.amount, group.currency)}</b></article>)}</div> : <div className="empty-state report-empty"><strong>Расходов нет</strong><span>Список появится после подтверждённых расходных операций.</span></div>}
    </section>

    {moneyTone(group.adjustment) !== "neutral" ? <p className="report-adjustment-note">Явные корректировки в Cash Flow: {formatMoney(group.adjustment, group.currency)}.</p> : null}
  </section>;
}

export function ReportsScreen({ onError, timezone }: ReportsScreenProps) {
  const fallbackMonth = useMemo(() => currentReportMonth(new Date(), timezone), [timezone]);
  const [month, setMonth] = useState(fallbackMonth);
  const [currency, setCurrency] = useState<ReportCurrencyFilter>("ALL");
  const [report, setReport] = useState<FinancialReport | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let mounted = true;
    queueMicrotask(() => {
      if (!mounted) return;
      const query = parseReportQuery(window.location.search, fallbackMonth);
      setMonth(query.month);
      setCurrency(query.currency);
      setReady(true);
    });
    return () => {
      mounted = false;
    };
  }, [fallbackMonth]);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      setReport(await apiClient.get<FinancialReport>(financialReportUrl(month, currency)));
    } catch (error) {
      onError(error);
    } finally {
      setIsLoading(false);
    }
  }, [currency, month, onError]);

  useEffect(() => {
    if (ready) queueMicrotask(() => void load());
  }, [load, ready]);

  function changeMonth(value: string) {
    if (!value) return;
    setMonth(value);
    syncReportUrl(value, currency);
  }

  function changeCurrency(value: ReportCurrencyFilter) {
    setCurrency(value);
    syncReportUrl(month, value);
  }

  return <section>
    <header className="screen-header reports-header">
      <div><span className="kicker">Аналитика · реальные операции</span><h1>Отчёты</h1><p>Доходы, расходы и денежный поток за {monthLabel(month, timezone)}. Валюты не смешиваются.</p></div>
      <div className="report-filter-bar">
        <label>Период<input aria-label="Период отчёта" onChange={(event) => changeMonth(event.target.value)} type="month" value={month}/></label>
        <label>Валюта<select aria-label="Валюта отчёта" onChange={(event) => changeCurrency(event.target.value as ReportCurrencyFilter)} value={currency}>{currencyOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <button className="secondary-button" disabled={isLoading} onClick={() => void load()} type="button">Обновить</button>
      </div>
    </header>

    {!ready || isLoading ? <ReportSkeleton/> : report?.groups.length ? <div className="reports-groups">{report.groups.map((group) => <ReportGroupView group={group} key={group.currency} timezone={timezone}/>)}</div> : <section className="panel feature-placeholder"><span className="feature-placeholder-icon" aria-hidden="true">∅</span><h2>За период нет данных</h2><p>В отчёт входят только подтверждённые и сверенные операции выбранного месяца.</p></section>}
  </section>;
}
