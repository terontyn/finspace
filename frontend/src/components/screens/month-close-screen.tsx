"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ActionDialog } from "@/components/ui/action-dialog";
import { EntityDrawer } from "@/components/ui/entity-drawer";
import { apiClient, ApiClientError } from "@/lib/api-client";
import { formatMoney, moneyTone } from "@/lib/money";
import type {
  MonthCloseAsClosedReport,
  MonthCloseComparison,
  MonthCloseHistoryPage,
  MonthCloseIssue,
  MonthClosePeriodSummary,
  MonthCloseRevision,
  MonthClosure,
  MonthClosurePage,
} from "@/types/automations";
import type { Currency, Money } from "@/types/finance";

const previousMonth = (() => {
  const value = new Date();
  value.setDate(1);
  value.setMonth(value.getMonth() - 1);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
})();

const statusLabels: Record<MonthClosePeriodSummary["status"], string> = {
  not_prepared: "Не подготовлен",
  draft: "Черновик",
  ready: "Готов",
  blocked: "Заблокирован",
  confirmed: "Закрыт",
  reopened: "Открыт повторно",
};

const issueLabels: Record<string, string> = {
  MONTH_CLOSE_PERIOD_NOT_ENDED: "Месяц ещё не завершён",
  DRAFT_TRANSACTIONS: "Черновики операций",
  SYNC_CONFLICTS_IN_PERIOD: "Конфликты синхронизации в периоде",
  MONTH_CLOSE_SEQUENCE_CONFLICT: "Нарушена последовательность закрытия",
  UNCATEGORIZED_TRANSACTIONS: "Операции без категории",
  POSSIBLE_DUPLICATES: "Возможные дубликаты",
  NEGATIVE_PERIOD_END_BALANCES: "Отрицательные остатки",
  ACCOUNT_NOT_RECONCILED: "Счета требуют сверки",
  FAILED_RECURRING_EXECUTIONS: "Ошибки регулярных операций",
  FAILED_SYNC_OUTBOX: "Ошибки доставки синхронизации",
  IMPORT_ROWS_REQUIRING_ATTENTION: "Строки импорта требуют внимания",
  BACKUP_MISSING: "Резервная копия отсутствует",
  BACKUP_UNVERIFIED: "Резервная копия не проверена",
  BACKUP_STALE: "Резервная копия устарела",
  STAGED_IMPORTS: "Есть незавершённые импорты",
  OUT_OF_PERIOD_SYNC_CONFLICTS: "Конфликты вне периода",
  NO_FINANCIAL_ACTIVITY: "Нет финансовой активности",
};

function newIntentKey(action: "confirm" | "reopen"): string {
  if (globalThis.crypto?.randomUUID) return `month-close-${action}-${globalThis.crypto.randomUUID()}`;
  return `month-close-${action}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

function monthKey(value: string): string {
  return value.slice(0, 7);
}

function periodEnd(value: string): string {
  const [year, month] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10);
}

function monthLabel(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", { month: "long", timeZone: "UTC", year: "numeric" })
    .format(new Date(`${monthKey(value)}-15T12:00:00Z`));
}

function dateTimeLabel(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function recordList(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
    : [];
}

function strictRecordList(
  value: unknown,
  validator: (item: Record<string, unknown>) => boolean,
): Array<Record<string, unknown>> | null {
  if (!Array.isArray(value)) return null;
  const result: Array<Record<string, unknown>> = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null || !validator(item as Record<string, unknown>)) {
      return null;
    }
    result.push(item as Record<string, unknown>);
  }
  return result;
}

function isMoneyString(value: unknown): value is Money {
  return typeof value === "string" && /^-?\d+(?:\.\d+)?$/.test(value);
}

function isCurrencyCode(value: unknown): value is Currency {
  return typeof value === "string" && /^[A-Z]{3}$/.test(value);
}

function isRequiredString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function historicalCurrencies(value: unknown): Array<Record<string, unknown>> | null {
  return strictRecordList(value, (item) =>
    isCurrencyCode(item.currency)
    && isMoneyString(item.income)
    && isMoneyString(item.expense)
    && isMoneyString(item.adjustment)
    && isMoneyString(item.net_cashflow)
    && isMoneyString(item.transfer_volume)
    && Number.isInteger(item.transactions_count),
  );
}

function historicalBalances(value: unknown): Array<Record<string, unknown>> | null {
  return strictRecordList(value, (item) =>
    isRequiredString(item.account_id)
    && isRequiredString(item.name)
    && isCurrencyCode(item.currency)
    && isMoneyString(item.opening_balance)
    && isMoneyString(item.balance),
  );
}

function historicalReconciliation(value: unknown): Array<Record<string, unknown>> | null {
  const accountTypes = new Set([
    "cash", "debit_card", "credit_card", "current_account", "savings", "deposit",
    "brokerage", "crypto_wallet", "other",
  ]);
  return strictRecordList(value, (item) => {
    const covered = item.covered;
    const state = item.state;
    return isRequiredString(item.account_id)
      && isRequiredString(item.account_name)
      && accountTypes.has(String(item.account_type))
      && isCurrencyCode(item.currency)
      && (item.period_end_balance === null || isMoneyString(item.period_end_balance))
      && (state === "reconciled" || state === "not_reconciled")
      && typeof covered === "boolean"
      && covered === (state === "reconciled")
      && isRequiredString(item.required_statement_date)
      && (item.latest_statement_date === null || isRequiredString(item.latest_statement_date))
      && (item.eligibility_reason === "period_activity" || item.eligibility_reason === "non_zero_period_end_balance")
      && typeof item.archived === "boolean";
  });
}

function stringValue(value: unknown, fallback = "—"): string {
  return typeof value === "string" ? value : fallback;
}

function currencyValue(value: unknown): Currency {
  return stringValue(value, "RUB") as Currency;
}

function moneyValue(value: unknown): Money {
  return stringValue(value, "0.0000") as Money;
}

function CurrencySummary({ groups, unavailable = false }: { groups: Array<Record<string, unknown>>; unavailable?: boolean }) {
  if (unavailable) return <div className="empty-state month-close-empty"><strong>Недоступно</strong><span>Исторический snapshot не содержит валютных итогов.</span></div>;
  if (!groups.length) return <div className="empty-state month-close-empty"><strong>Финансовой активности нет</strong><span>Для валютных итогов нужны подтверждённые операции этого месяца.</span></div>;
  return <div className="month-close-currency-grid">{groups.map((group) => {
    const currency = currencyValue(group.currency);
    const net = moneyValue(group.net_cashflow);
    return <article key={currency}><header><span>{currency}</span><small>{String(group.transactions_count ?? 0)} операций</small></header><dl><div><dt>Доходы</dt><dd className="money--positive">{formatMoney(moneyValue(group.income), currency)}</dd></div><div><dt>Расходы</dt><dd className="money--negative">{formatMoney(moneyValue(group.expense), currency)}</dd></div><div><dt>Cash Flow</dt><dd className={`money--${moneyTone(net)}`}>{formatMoney(net, currency)}</dd></div><div><dt>Переводы</dt><dd>{formatMoney(moneyValue(group.transfer_volume), currency)}</dd></div></dl></article>;
  })}</div>;
}

function IssueGroup({ items, severity, title }: { items: MonthCloseIssue[]; severity: MonthCloseIssue["severity"]; title: string }) {
  return <section aria-label={title} className={`month-close-issue-group month-close-issue-group--${severity}`}><header><div><span className="kicker">{severity}</span><h3>{title}</h3></div><span className="count-badge">{items.length}</span></header>{items.length ? <div>{items.map((item) => <article key={`${item.code}-${item.scope}`}><div><strong>{issueLabels[item.code] ?? item.message}</strong><small>{item.message}</small></div><span>{item.count}</span><code>{item.code}</code></article>)}</div> : <p>Нет.</p>}</section>;
}

function ReconciliationCoverage({ unavailable = false, value }: { unavailable?: boolean; value: unknown }) {
  const items = recordList(value);
  if (unavailable) return <section className="panel month-close-reconciliation"><div className="panel-heading"><div><span className="kicker">Отдельный bounded context</span><h2>Покрытие сверками</h2></div></div><div className="empty-state month-close-empty"><strong>Недоступно</strong><span>Исторический snapshot не содержит reconciliation coverage.</span></div></section>;
  return <section className="panel month-close-reconciliation"><div className="panel-heading"><div><span className="kicker">Отдельный bounded context</span><h2>Покрытие сверками</h2><p>Сверка не меняется при закрытии: здесь только evidence на конец месяца или позже.</p></div><span className="count-badge">{items.filter((item) => item.state === "reconciled").length}/{items.length}</span></div>{items.length ? <div className="month-close-coverage-list">{items.map((item) => <article key={stringValue(item.account_id)}><div><strong>{stringValue(item.account_name, "Счёт")}</strong><small>{stringValue(item.currency)} · {item.archived ? "архивный" : "активный"}</small></div><span className={`status-chip status-chip--${item.state === "reconciled" ? "reconciled" : "warning"}`}>{item.state === "reconciled" ? "Сверен" : "Нужна сверка"}</span><small>{item.latest_statement_date ? `Выписка: ${stringValue(item.latest_statement_date)}` : `Нужна дата ≥ ${stringValue(item.required_statement_date)}`}</small></article>)}</div> : <div className="empty-state month-close-empty"><strong>Сверка не требуется</strong><span>Нет счетов с активностью или ненулевым остатком, для которых нужна evidence.</span></div>}</section>;
}

function AccountBalances({ unavailable = false, value }: { unavailable?: boolean; value: unknown }) {
  const balances = recordList(value);
  if (unavailable) return <section className="panel month-close-balances"><div className="panel-heading"><div><span className="kicker">Period end</span><h2>Остатки счетов</h2></div></div><div className="empty-state month-close-empty"><strong>Недоступно</strong><span>Исторический snapshot не содержит остатков счетов.</span></div></section>;
  return <section className="panel month-close-balances"><div className="panel-heading"><div><span className="kicker">Period end</span><h2>Остатки счетов</h2></div><span className="count-badge">{balances.length}</span></div>{balances.length ? <div>{balances.map((item) => {
    const currency = currencyValue(item.currency);
    return <article key={stringValue(item.account_id)}><div><strong>{stringValue(item.name, "Счёт")}</strong><small>{currency}</small></div><b>{formatMoney(moneyValue(item.balance), currency)}</b></article>;
  })}</div> : <div className="empty-state month-close-empty"><strong>Счетов на дату нет</strong><span>Backend не вернул остатки для этого cutoff.</span></div>}</section>;
}

function MonthCloseSkeleton() {
  return <div aria-label="Загружаем закрытие месяца" className="month-close-skeleton">{Array.from({ length: 10 }, (_, index) => <i key={index}/>)}</div>;
}

export function MonthCloseScreen({ onError }: { onError: (error: unknown) => void }) {
  const [period, setPeriod] = useState(previousMonth);
  const [page, setPage] = useState<MonthClosurePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"prepare" | "confirm" | "reopen" | null>(null);
  const [dialog, setDialog] = useState<"confirm" | "reopen" | null>(null);
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [history, setHistory] = useState<MonthCloseRevision[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [revisionReport, setRevisionReport] = useState<MonthCloseAsClosedReport | null>(null);
  const [comparison, setComparison] = useState<MonthCloseComparison | null>(null);
  const [revisionOpen, setRevisionOpen] = useState(false);
  const confirmKey = useRef<string | null>(null);
  const reopenKey = useRef<string | null>(null);
  const reopenPayload = useRef<string | null>(null);
  const actionInFlight = useRef(false);
  const historyRequest = useRef(0);
  const revisionRequest = useRef(0);

  const load = useCallback(async () => {
    try {
      const result = await apiClient.get<MonthClosurePage>("/api/v1/month-close?limit=120");
      setPage(result);
      setLoadError(null);
      setPeriod((current) => result.periods.some((item) => monthKey(item.period_month) === current)
        ? current
        : monthKey(result.periods[0]?.period_month ?? previousMonth));
    } catch (error) {
      setLoadError("Не удалось загрузить состояние закрытия месяца.");
      onError(error);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  const selected = useMemo(
    () => page?.items.find((item) => monthKey(item.period_month) === period) ?? null,
    [page, period],
  );
  const periodSummary = useMemo(
    () => page?.periods.find((item) => monthKey(item.period_month) === period) ?? null,
    [page, period],
  );

  const loadHistory = useCallback(async (closure: MonthClosure | null, request: number) => {
    if (!closure?.capabilities.can_view_history || !closure.current_revision) {
      if (historyRequest.current === request) {
        setHistory([]);
        setHistoryLoading(false);
      }
      return;
    }
    if (historyRequest.current === request) setHistoryLoading(true);
    try {
      const [year, month] = monthKey(closure.period_month).split("-");
      const result = await apiClient.get<MonthCloseHistoryPage>(`/api/v1/month-close/${year}/${Number(month)}/history?limit=100&order=newest`);
      if (historyRequest.current === request) setHistory(result.items);
    } catch (error) {
      if (historyRequest.current === request) onError(error);
    } finally {
      if (historyRequest.current === request) setHistoryLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    const request = ++historyRequest.current;
    queueMicrotask(() => void loadHistory(selected, request));
    return () => {
      if (historyRequest.current === request) historyRequest.current += 1;
    };
  }, [loadHistory, selected]);

  async function refreshAfterMutation(result: MonthClosure) {
    setPeriod(monthKey(result.period_month));
    await load();
  }

  function clearIntent(action: "confirm" | "reopen") {
    if (action === "confirm") confirmKey.current = null;
    else {
      reopenKey.current = null;
      reopenPayload.current = null;
    }
  }

  function handleActionError(error: unknown, action: "confirm" | "reopen") {
    if (error instanceof ApiClientError && error.code === "MONTH_CLOSE_PREVIEW_STALE") {
      setNotice("Данные изменились после preview. Состояние обновлено — выполните подготовку заново.");
      setDialog(null);
      clearIntent(action);
      void load();
      return;
    }
    if (error instanceof ApiClientError && error.code === "MONTH_CLOSE_VERSION_CONFLICT") {
      setNotice("Версия закрытия устарела. Получены актуальные данные; повторите явное действие.");
      setDialog(null);
      clearIntent(action);
      void load();
      return;
    }
    if (error instanceof ApiClientError && error.status === 403) {
      setNotice("У вашей роли нет права выполнить это действие.");
      setDialog(null);
      clearIntent(action);
      return;
    }
    if (error instanceof ApiClientError && error.status > 0) clearIntent(action);
    onError(error);
  }

  async function prepare() {
    if (actionInFlight.current) return;
    actionInFlight.current = true;
    const [year, month] = period.split("-");
    setBusy("prepare");
    setNotice(null);
    try {
      await refreshAfterMutation(await apiClient.post<MonthClosure>(`/api/v1/month-close/${year}/${Number(month)}/prepare`, {}));
    } catch (error) {
      onError(error);
    } finally {
      actionInFlight.current = false;
      setBusy(null);
    }
  }

  function openConfirm() {
    confirmKey.current = newIntentKey("confirm");
    setDialog("confirm");
  }

  async function confirm() {
    if (!selected?.prepare_token || actionInFlight.current) return;
    actionInFlight.current = true;
    confirmKey.current ??= newIntentKey("confirm");
    const [year, month] = monthKey(selected.period_month).split("-");
    setBusy("confirm");
    try {
      const result = await apiClient.post<MonthClosure>(
        `/api/v1/month-close/${year}/${Number(month)}/confirm`,
        { version: selected.version, confirm: true, prepare_token: selected.prepare_token },
        { "X-Idempotency-Key": confirmKey.current },
      );
      confirmKey.current = null;
      setDialog(null);
      await refreshAfterMutation(result);
    } catch (error) {
      handleActionError(error, "confirm");
    } finally {
      actionInFlight.current = false;
      setBusy(null);
    }
  }

  function openReopen() {
    reopenKey.current = null;
    reopenPayload.current = null;
    setReason("");
    setDialog("reopen");
  }

  async function reopen() {
    if (!selected || reason.trim().length < 3 || actionInFlight.current) return;
    actionInFlight.current = true;
    const normalizedReason = reason.trim();
    const payloadIdentity = `${selected.version}:${normalizedReason}`;
    if (reopenPayload.current !== payloadIdentity) {
      reopenKey.current = newIntentKey("reopen");
      reopenPayload.current = payloadIdentity;
    }
    reopenKey.current ??= newIntentKey("reopen");
    const [year, month] = monthKey(selected.period_month).split("-");
    setBusy("reopen");
    try {
      const result = await apiClient.post<MonthClosure>(
        `/api/v1/month-close/${year}/${Number(month)}/reopen`,
        { version: selected.version, reason: normalizedReason },
        { "X-Idempotency-Key": reopenKey.current },
      );
      reopenKey.current = null;
      reopenPayload.current = null;
      setDialog(null);
      await refreshAfterMutation(result);
    } catch (error) {
      handleActionError(error, "reopen");
    } finally {
      actionInFlight.current = false;
      setBusy(null);
    }
  }

  async function openRevision(revision: MonthCloseRevision) {
    const request = ++revisionRequest.current;
    const [year, month] = monthKey(revision.period_month).split("-");
    setRevisionOpen(true);
    setRevisionReport(null);
    setComparison(null);
    try {
      const base = `/api/v1/month-close/${year}/${Number(month)}/history/${revision.revision_number}`;
      const [report, currentComparison] = await Promise.all([
        apiClient.get<MonthCloseAsClosedReport>(`${base}/report`),
        apiClient.get<MonthCloseComparison>(`${base}/comparison`),
      ]);
      if (revisionRequest.current !== request) return;
      setRevisionReport(report);
      setComparison(currentComparison);
    } catch (error) {
      if (revisionRequest.current !== request) return;
      setRevisionOpen(false);
      onError(error);
    }
  }

  function closeRevision() {
    revisionRequest.current += 1;
    setRevisionOpen(false);
    setRevisionReport(null);
    setComparison(null);
  }

  function selectPeriod(nextPeriod: string) {
    historyRequest.current += 1;
    closeRevision();
    setHistory([]);
    setPeriod(nextPeriod);
  }

  if (loading) return <MonthCloseSkeleton />;
  if (loadError && !page) return <section className="panel month-close-load-error" role="alert"><strong>{loadError}</strong><button className="secondary-button" onClick={() => { setLoading(true); void load(); }} type="button">Повторить</button></section>;

  const currencies = recordList(selected?.summary.currencies);
  const prepareAllowed = periodSummary?.capabilities.can_prepare ?? false;
  const closedCurrencies = revisionReport ? historicalCurrencies(revisionReport.currencies) : null;
  const closedBalances = revisionReport ? historicalBalances(revisionReport.account_balances) : null;
  const closedReconciliation = revisionReport
    ? historicalReconciliation(revisionReport.reconciliation_coverage)
    : null;
  const closedCurrenciesUnavailable = revisionReport !== null && closedCurrencies === null;
  const closedBalancesUnavailable = revisionReport !== null && closedBalances === null;
  const closedReconciliationUnavailable = revisionReport !== null && closedReconciliation === null;

  return <section className="month-close-screen">
    <header className="screen-header month-close-header"><div><span className="kicker">Hard close · проверяемые снимки</span><h1>Закрытие месяца</h1><p>Подготовка ничего не исправляет. После подтверждения финансовые изменения до cutoff блокируются, а revision остаётся неизменной.</p></div><div className="month-close-header-state"><span>Закрыто по</span><strong>{page?.closed_through ?? "не закрывалось"}</strong><small>Backup policy: {page?.backup_policy === "require_healthy" ? "требуется healthy" : "предупреждать"}</small></div></header>

    {notice ? <div className="notice notice--warning" role="status"><span>{notice}</span><button className="text-button" onClick={() => setNotice(null)} type="button">Закрыть</button></div> : null}

    <div className="month-close-workspace">
      <aside className="panel month-close-periods"><div className="panel-heading"><div><span className="kicker">Периоды</span><h2>Текущий год и история</h2></div></div><label className="month-close-picker">Выбрать месяц<input max={new Date().toISOString().slice(0, 7)} onChange={(event) => selectPeriod(event.target.value)} type="month" value={period}/></label><div className="month-close-period-list">{page?.periods.map((item) => <button aria-pressed={monthKey(item.period_month) === period} className={monthKey(item.period_month) === period ? "month-close-period month-close-period--active" : "month-close-period"} key={item.period_month} onClick={() => selectPeriod(monthKey(item.period_month))} type="button"><div><strong>{monthLabel(item.period_month)}</strong><small>{item.current_revision ? `Revision ${item.current_revision}` : item.prepared ? `Версия ${item.version}` : "Не готовился"}</small></div><span className={`month-close-status month-close-status--${item.status}`}>{statusLabels[item.status]}</span>{item.blocker_count || item.warning_count ? <small>{item.blocker_count} блок. · {item.warning_count} предупр.</small> : null}</button>)}</div>{!page?.items.length ? <div className="empty-state month-close-empty"><strong>Подготовленных месяцев нет</strong><span>Выберите завершённый период и запустите prepare.</span></div> : null}</aside>

      <main className="month-close-content">
        <section className="panel month-close-selected"><div className="panel-heading"><div><span className="kicker">Выбранный период</span><h2>{monthLabel(`${period}-01`)}</h2><p>{selected ? `Backend preview · версия ${selected.version}` : "Снимок ещё не сформирован"}</p></div><span className={`month-close-status month-close-status--${periodSummary?.status ?? "not_prepared"}`}>{statusLabels[periodSummary?.status ?? "not_prepared"]}</span></div>
          {!selected ? <div className="month-close-unprepared"><div><strong>Месяц не подготовлен</strong><p>{prepareAllowed ? "Backend проверит ledger, синхронизацию, сверки и backup policy." : "Текущий или будущий месяц нельзя закрыть."}</p></div>{prepareAllowed ? <button className="primary-button" disabled={busy !== null} onClick={() => void prepare()} type="button">{busy === "prepare" ? "Проверяем…" : "Подготовить"}</button> : null}</div> : <>
            <div className="month-close-facts"><article><span>Операций</span><strong>{String(selected.summary.transaction_count ?? 0)}</strong><small>confirmed + reconciled</small></article><article><span>Блокировок</span><strong>{selected.blocking_issues?.length ?? 0}</strong><small>confirm запрещён</small></article><article><span>Предупреждений</span><strong>{selected.warning_issues?.length ?? 0}</strong><small>решение owner</small></article><article><span>Backup</span><strong>{stringValue(selected.summary.backup_status)}</strong><small>{stringValue(selected.summary.backup_verified_at, "проверка не подтверждена")}</small></article></div>
            <CurrencySummary groups={currencies}/>
            <div className="month-close-issues-grid"><IssueGroup items={selected.blocking_issues ?? []} severity="blocker" title="Блокировки"/><IssueGroup items={selected.warning_issues ?? []} severity="warning" title="Предупреждения"/><IssueGroup items={selected.info_issues ?? []} severity="info" title="Информация"/></div>
            <div className="month-close-actions"><div><strong>{selected.status === "confirmed" ? "Период защищён" : selected.status === "reopened" ? "Период открыт для поправок" : "Preview можно обновить"}</strong><span>{selected.status === "confirmed" ? `Revision ${selected.current_revision} хранит immutable snapshot.` : "Повторный prepare пересчитает только backend-данные."}</span></div>{selected.capabilities.can_prepare ? <button className="secondary-button" disabled={busy !== null} onClick={() => void prepare()} type="button">{busy === "prepare" ? "Проверяем…" : "Обновить preview"}</button> : null}{selected.capabilities.can_confirm ? <button className="primary-button" disabled={busy !== null} onClick={openConfirm} type="button">Подтвердить закрытие</button> : null}{selected.capabilities.can_reopen ? <button className="danger-button" disabled={busy !== null} onClick={openReopen} type="button">Открыть повторно</button> : null}</div>
          </>}
        </section>

        {selected ? <div className="month-close-details"><AccountBalances value={selected.summary.account_balances}/><ReconciliationCoverage value={selected.summary.reconciliation_coverage}/></div> : null}

        {selected?.current_revision ? <section className="panel month-close-history"><div className="panel-heading"><div><span className="kicker">Immutable history</span><h2>Revisions</h2><p>Каждая строка — отдельное подтверждённое состояние, которое не меняется после reopen.</p></div><span className="count-badge">{history.length}</span></div>{historyLoading ? <div className="month-close-history-loading">Загружаем историю…</div> : <div className="month-close-history-list">{history.map((revision) => <article key={revision.id}><span className="month-close-revision-number">{revision.revision_number}</span><div><strong>Revision {revision.revision_number}</strong><small>{dateTimeLabel(revision.confirmed_at)} · {revision.confirmed_by.display_name} (текущий профиль)</small>{revision.reopened ? <small>Открыта повторно: {revision.reopened.reason ?? "причина сохранена в audit"}</small> : null}</div><div>{revision.legacy_unverified ? <span className="status-chip status-chip--warning">Legacy unverified</span> : <code>{revision.financial_fingerprint?.slice(0, 12)}…</code>}<button className="text-button" onClick={() => void openRevision(revision)} type="button">Открыть снимок →</button></div></article>)}</div>}</section> : null}
      </main>
    </div>

    {dialog === "confirm" && selected ? <ActionDialog description="Backend повторно проверит preview, fingerprint и блокировки. Автоподтверждения при stale-состоянии нет." eyebrow="Необратимый cutoff без reopen" onClose={() => { if (!busy) { setDialog(null); confirmKey.current = null; } }} title={`Закрыть ${monthLabel(selected.period_month)}?`}><div className="month-close-dialog-body"><CurrencySummary groups={currencies}/><dl className="month-close-confirm-facts"><div><dt>Предупреждений</dt><dd>{selected.warning_issues?.length ?? 0}</dd></div><div><dt>Fingerprint</dt><dd><code>{selected.prepared_fingerprint?.slice(0, 16)}…</code></dd></div><div><dt>Защищённый cutoff</dt><dd>{periodEnd(selected.period_month)}</dd></div></dl><div className="month-close-dialog-warning">Операции до {periodEnd(selected.period_month)} будут защищены от изменения.</div></div><footer><button className="secondary-button" disabled={busy !== null} onClick={() => { setDialog(null); confirmKey.current = null; }} type="button">Отмена</button><button className="primary-button" disabled={busy !== null} onClick={() => void confirm()} type="button">{busy === "confirm" ? "Подтверждаем…" : "Подтвердить закрытие"}</button></footer></ActionDialog> : null}

    {dialog === "reopen" && selected ? <ActionDialog description="Разрешено открыть только последний закрытый месяц. Revision останется в истории, а причина — в audit." eyebrow="Owner action" onClose={() => { if (!busy) { setDialog(null); clearIntent("reopen"); } }} title={`Открыть revision ${selected.current_revision}?`}><div className="month-close-dialog-body"><div className="month-close-dialog-warning month-close-dialog-warning--danger">После открытия финансовые операции периода снова можно будет менять до следующего confirm.</div><label className="month-close-reason">Обязательная причина<textarea autoComplete="off" maxLength={500} minLength={3} onChange={(event) => setReason(event.target.value)} placeholder="Что требуется исправить и кто это согласовал" required rows={4} value={reason}/><small>{reason.trim().length}/500 · минимум 3 символа</small></label></div><footer><button className="secondary-button" disabled={busy !== null} onClick={() => { setDialog(null); clearIntent("reopen"); }} type="button">Отмена</button><button className="danger-button" disabled={busy !== null || reason.trim().length < 3} onClick={() => void reopen()} type="button">{busy === "reopen" ? "Открываем…" : "Открыть месяц"}</button></footer></ActionDialog> : null}

    {revisionOpen ? <EntityDrawer ariaLabel="Исторический снимок закрытия месяца" eyebrow="As closed · read only" onClose={closeRevision} subtitle={revisionReport ? `Подтверждено ${dateTimeLabel(revisionReport.confirmed_at)}` : "Загружаем immutable snapshot"} title={revisionReport ? `Revision ${revisionReport.revision_number}` : "Исторический снимок"}>{revisionReport ? <div className="month-close-revision-detail">{revisionReport.legacy_unverified ? <div className="notice notice--warning" role="alert">Историческое закрытие создано до введения проверяемых снимков Finspace.</div> : <div className="month-close-fingerprint"><span>Financial fingerprint</span><code>{revisionReport.financial_fingerprint}</code></div>}<section><span className="kicker">Закрыто в revision {revisionReport.revision_number}</span><CurrencySummary groups={closedCurrencies ?? []} unavailable={closedCurrenciesUnavailable}/></section><section><span className="kicker">Текущие данные</span><CurrencySummary groups={recordList(comparison?.current.currencies)}/></section>{comparison ? <div className="month-close-comparison-state"><strong>{closedCurrenciesUnavailable || comparison.unavailable_sections.includes("currencies") ? "Сравнение валютных итогов недоступно" : comparison.differences.currencies.some((item) => item.changed) ? "Текущие итоги отличаются" : "Валютные итоги совпадают"}</strong><span>{closedCurrenciesUnavailable || comparison.unavailable_sections.includes("currencies") ? "Legacy snapshot не содержит достаточных данных; current ledger не подставляется вместо истории." : "Сравнение выполняется отдельно по каждой валюте. Валюты не складываются."}</span></div> : null}<AccountBalances unavailable={closedBalancesUnavailable} value={closedBalances ?? []}/><ReconciliationCoverage unavailable={closedReconciliationUnavailable} value={closedReconciliation ?? []}/></div> : <div className="month-close-history-loading">Загружаем snapshot и сравнение…</div>}</EntityDrawer> : null}
  </section>;
}
