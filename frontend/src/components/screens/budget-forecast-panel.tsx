"use client";

import Link from "next/link";

import { EntityDrawer } from "@/components/ui/entity-drawer";
import {
  areForecastInstantsEqual,
  forecastPeriodMessage,
  formatForecastTimestamp,
  isZeroForecastMoney,
} from "@/lib/budget-forecast";
import { formatBudgetMoney, isNegativeMoney } from "@/lib/budget";
import type {
  BudgetForecastMode,
  BudgetForecastOccurrence,
  BudgetForecastResponse,
} from "@/types/budget-forecast";

export interface BudgetForecastViewState {
  data: BudgetForecastResponse | null;
  error: string | null;
  key: string | null;
  loading: boolean;
  stale: boolean;
  unavailable: boolean;
}

export interface BudgetForecastDetailsState {
  data: BudgetForecastResponse | null;
  error: string | null;
  key: string | null;
  loading: boolean;
}

const modeLabels: Record<BudgetForecastMode, string> = {
  confirmed: "Подтверждённые",
  draft: "Черновики",
};

const occurrenceStateLabels: Record<BudgetForecastOccurrence["state"], string> = {
  advisory: "Напоминание",
  exception: "Исключение",
  informational_transfer: "Перевод",
  pending_draft: "Черновик",
  scheduled: "Запланировано",
};

function MoneyFact({ currency, label, value }: { currency: string; label: string; value: string }) {
  return <div><dt>{label}</dt><dd className={isNegativeMoney(value) ? "money--negative" : undefined}>{formatBudgetMoney(value, currency)}</dd></div>;
}

function ProjectionColumn({
  currency,
  eyebrow,
  expense,
  income,
  netCashflow,
}: {
  currency: string;
  eyebrow: string;
  expense: string;
  income: string;
  netCashflow: string;
}) {
  return <article className="budget-forecast-column"><span className="kicker">{eyebrow}</span><dl><MoneyFact currency={currency} label="Доход" value={income}/><MoneyFact currency={currency} label="Расход" value={expense}/><MoneyFact currency={currency} label="Net cash flow" value={netCashflow}/></dl></article>;
}

function ExceptionSummary({ forecast }: { forecast: BudgetForecastResponse }) {
  if (!forecast.exceptions.count) return null;
  const parts = [
    [forecast.exceptions.failed_count, "ошибок"],
    [forecast.exceptions.skipped_count, "пропусков"],
    [forecast.exceptions.overdue_count, "просрочено"],
    [forecast.exceptions.incomplete_count, "неполных правил"],
    [forecast.exceptions.blocked_rule_count, "заблокировано"],
    [forecast.exceptions.materialized_excluded_count, "уже материализовано"],
  ].filter(([count]) => count !== 0).map(([count, label]) => `${count} ${label}`);
  return <div className="budget-forecast-warning" role="status"><div><strong>Прогноз содержит исключения: {forecast.exceptions.count}</strong><span>{parts.join(" · ")}</span></div><Link href="/recurring">Проверить регулярные</Link></div>;
}

export function BudgetForecastPanel({
  currency,
  onOpenOccurrences,
  onRetry,
  state,
}: {
  currency: string;
  onOpenOccurrences: () => void;
  onRetry: () => void;
  state: BudgetForecastViewState;
}) {
  if (state.loading && !state.data) {
    return <section aria-label="Рассчитываем прогноз бюджета" className="panel budget-forecast-panel budget-forecast-loading"><i/><i/><i/></section>;
  }
  if (state.unavailable) {
    return <section className="budget-forecast-unavailable" role="status">Прогноз регулярных операций пока не поддерживается этой версией backend.</section>;
  }
  if (state.error && !state.data) {
    return <section className="panel budget-forecast-error" role="alert"><div><strong>Прогноз не загрузился</strong><span>{state.error}</span></div><button className="secondary-button" onClick={onRetry} type="button">Повторить</button></section>;
  }
  if (!state.data) return null;

  const forecast = state.data;
  const periodMessage = forecastPeriodMessage(forecast.period_state);
  const hasOccurrenceDetails = forecast.forecast.occurrence_count > 0
    || forecast.advisory.occurrence_count > 0
    || forecast.informational_transfers.occurrence_count > 0
    || forecast.exceptions.count > 0;

  return <section className={`panel budget-forecast-panel ${state.stale ? "budget-forecast-panel--stale" : ""}`}>
    <div className="panel-heading budget-forecast-heading"><div><span className="kicker">Регулярные операции</span><h2>Прогноз до конца месяца</h2><p>Факт, будущие вхождения и ожидаемый итог рассчитаны backend.</p></div><div><span className="budget-forecast-asof">По состоянию на {formatForecastTimestamp(forecast.as_of, forecast.timezone)}</span><button aria-label="Обновить прогноз" className="text-button" disabled={state.loading} onClick={onRetry} type="button">{state.loading ? "Обновляем…" : "Обновить"}</button></div></div>
    {state.stale ? <div className="budget-forecast-stale" role="status">Показан предыдущий успешный расчёт. {state.error}</div> : null}
    {periodMessage ? <div className="budget-forecast-period-note" role="status">{periodMessage}</div> : null}

    <div className="budget-forecast-columns">
      <ProjectionColumn currency={currency} eyebrow="Факт" expense={forecast.actual.expense} income={forecast.actual.income} netCashflow={forecast.actual.net_cashflow}/>
      <ProjectionColumn currency={currency} eyebrow="Будущие вхождения" expense={forecast.forecast.expense} income={forecast.forecast.income} netCashflow={forecast.forecast.net_cashflow}/>
      <ProjectionColumn currency={currency} eyebrow="Ожидаемый итог" expense={forecast.projected.expense} income={forecast.projected.income} netCashflow={forecast.projected.net_cashflow}/>
    </div>

    <div className="budget-forecast-meta-grid">
      <article><span className="kicker">Режимы</span>{forecast.forecast.mode_breakdown.filter((mode) => mode.occurrence_count > 0).length ? <><dl>{forecast.forecast.mode_breakdown.filter((mode) => mode.occurrence_count > 0).map((mode) => <div key={mode.mode}><dt>{modeLabels[mode.mode]} · {mode.occurrence_count}</dt><dd>{formatBudgetMoney(mode.expense, currency)} расходов</dd></div>)}</dl>{forecast.forecast.pending_draft_occurrence_count > 0 ? <p>Черновики могут требовать подтверждения.</p> : null}</> : <p>Регулярных операций до конца периода нет.</p>}</article>
      <article><span className="kicker">Отдельно от прогноза</span><dl><div><dt>Напоминания · {forecast.advisory.occurrence_count}</dt><dd>доход {formatBudgetMoney(forecast.advisory.income, currency)} · расход {formatBudgetMoney(forecast.advisory.expense, currency)}</dd></div><div><dt>Переводы · {forecast.informational_transfers.occurrence_count}</dt><dd>{formatBudgetMoney(forecast.informational_transfers.volume, currency)}</dd></div></dl></article>
    </div>

    {!isZeroForecastMoney(forecast.unbudgeted_forecast_expense) ? <div className="budget-forecast-warning" role="status"><div><strong>Прогноз вне бюджета: {formatBudgetMoney(forecast.unbudgeted_forecast_expense, currency)}</strong><span>Backend не нашёл точного Budget allocation для этих расходов.</span></div></div> : null}
    <ExceptionSummary forecast={forecast}/>
    {hasOccurrenceDetails ? <footer><button className="secondary-button" onClick={onOpenOccurrences} type="button">Показать операции прогноза</button><span>Только просмотр · управление правилами находится в разделе «Регулярные».</span></footer> : null}
  </section>;
}

function OccurrenceCard({ occurrence, timezone }: { occurrence: BudgetForecastOccurrence; timezone: string }) {
  const dateMoved = !areForecastInstantsEqual(occurrence.scheduled_for, occurrence.effective_at);
  return <article className={`budget-occurrence-card budget-occurrence-card--${occurrence.state}`}>
    <header><div><strong>{occurrence.rule_name}</strong><span>{occurrenceStateLabels[occurrence.state]}</span></div><b>{formatBudgetMoney(occurrence.amount, occurrence.currency)}</b></header>
    <dl>
      <div><dt>Дата в бюджете</dt><dd>{formatForecastTimestamp(occurrence.effective_at, timezone)}</dd></div>
      {dateMoved ? <div><dt>Запланировано</dt><dd>{formatForecastTimestamp(occurrence.scheduled_for, timezone)}</dd></div> : null}
      <div><dt>Режим</dt><dd>{occurrence.rule_mode}</dd></div>
      <div><dt>Тип</dt><dd>{occurrence.transaction_type}</dd></div>
      <div><dt>Категория</dt><dd>{occurrence.category_name ?? "Без категории"}</dd></div>
      <div><dt>Часовой пояс правила</dt><dd>{occurrence.rule_timezone}</dd></div>
      <div><dt>Источник суммы</dt><dd>{occurrence.amount_source === "linked_transaction" ? "Связанная операция" : "Правило"}</dd></div>
      {occurrence.execution_status ? <div><dt>Execution</dt><dd>{occurrence.execution_status}</dd></div> : null}
      {occurrence.transaction_status ? <div><dt>Операция</dt><dd>{occurrence.transaction_status}</dd></div> : null}
    </dl>
    {occurrence.reason ? <p>{occurrence.reason}</p> : null}
  </article>;
}

export function BudgetForecastOccurrencesDrawer({
  onClose,
  onRetry,
  state,
  timezone,
}: {
  onClose: () => void;
  onRetry: () => void;
  state: BudgetForecastDetailsState;
  timezone: string;
}) {
  return <EntityDrawer ariaLabel="Вхождения прогноза бюджета" eyebrow="Backend occurrences" onClose={onClose} subtitle="Только просмотр — без скрытых mutation-команд" title="Вхождения прогноза">
    <div className="budget-occurrence-drawer">
      {state.loading && !state.data ? <div className="budget-history-loading">Загружаем вхождения…</div> : null}
      {state.error && !state.data ? <div className="notice notice--error" role="alert"><span>{state.error}</span><button className="secondary-button" onClick={onRetry} type="button">Повторить</button></div> : null}
      {state.data && state.data.occurrences.length ? <div className="budget-occurrence-list">{state.data.occurrences.map((occurrence, index) => <OccurrenceCard key={`${occurrence.rule_id}-${occurrence.execution_id ?? occurrence.scheduled_for}-${index}`} occurrence={occurrence} timezone={timezone}/>)}</div> : null}
      {state.data && !state.data.occurrences.length ? <div className="empty-state"><strong>Вхождений нет</strong><span>Backend не вернул детальные occurrence для этого периода.</span></div> : null}
      <footer><Link className="secondary-button" href="/recurring">Открыть регулярные</Link></footer>
    </div>
  </EntityDrawer>;
}
