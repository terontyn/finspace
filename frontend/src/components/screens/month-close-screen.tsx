"use client";

import { useCallback, useEffect, useState } from "react";

import { apiClient } from "@/lib/api-client";
import type { ApiPage, MonthClosure } from "@/types/automations";

const previousMonth = (() => {
  const value = new Date();
  value.setDate(1);
  value.setMonth(value.getMonth() - 1);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
})();

export function MonthCloseScreen({ onError }: { onError: (error: unknown) => void }) {
  const [period, setPeriod] = useState(previousMonth);
  const [closures, setClosures] = useState<MonthClosure[]>([]);
  const [selected, setSelected] = useState<MonthClosure | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await apiClient.get<ApiPage<MonthClosure>>("/api/v1/month-close?limit=24");
      setClosures(result.items);
      setSelected((current) => current ? result.items.find((item) => item.id === current.id) ?? current : result.items[0] ?? null);
    } catch (error) {
      onError(error);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  async function prepare() {
    const [year, month] = period.split("-");
    setBusy(true);
    try {
      const result = await apiClient.post<MonthClosure>(`/api/v1/month-close/${year}/${Number(month)}/prepare`, {});
      setSelected(result);
      await load();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!selected || !window.confirm("Подтвердить закрытие месяца? Будет повторно проверен backup и блокировки.")) return;
    const [year, month] = selected.period_month.slice(0, 7).split("-");
    setBusy(true);
    try {
      const result = await apiClient.post<MonthClosure>(`/api/v1/month-close/${year}/${Number(month)}/confirm`, { version: selected.version, confirm: true });
      setSelected(result);
      await load();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }

  async function reopen() {
    if (!selected) return;
    const reason = window.prompt("Причина повторного открытия месяца:");
    if (!reason || reason.trim().length < 3) return;
    const [year, month] = selected.period_month.slice(0, 7).split("-");
    setBusy(true);
    try {
      const result = await apiClient.post<MonthClosure>(
        `/api/v1/month-close/${year}/${Number(month)}/reopen`,
        { version: selected.version, reason: reason.trim() },
      );
      setSelected(result);
      await load();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <header className="screen-header"><div><span className="kicker">Проверка без автоисправлений</span><h1>Закрытие месяца</h1><p>Подготовка показывает блокировки и предупреждения. Подтверждение доступно только owner.</p></div><div className="period-control"><input type="month" value={period} onChange={(event) => setPeriod(event.target.value)} /><button className="secondary-button" type="button" disabled={busy} onClick={() => void prepare()}>Подготовить</button></div></header>
      <div className="two-column conflict-layout">
        <article className="panel">
          <div className="panel-heading"><div><span className="kicker">История</span><h2>Периоды</h2></div></div>
          <div className="card-list">
            {closures.map((closure) => <button className={selected?.id === closure.id ? "conflict-card conflict-card--active" : "conflict-card"} type="button" key={closure.id} onClick={() => setSelected(closure)}><strong>{closure.period_month.slice(0, 7)}</strong><span>{closure.status} · версия {closure.version}</span></button>)}
            {!closures.length ? <div className="empty-state">Закрытия ещё не готовились.</div> : null}
          </div>
        </article>

        <article className="panel">
          {selected ? <>
            <div className="panel-heading"><div><span className="kicker">Preview</span><h2>{selected.period_month.slice(0, 7)}</h2></div><span className={`sync-state ${selected.status === "blocked" ? "sync-state--error" : selected.status === "reopened" ? "sync-state--paused" : ""}`}>{selected.status}</span></div>
            <div className="close-metrics">
              <div><span>Операций</span><strong>{String(selected.summary.transaction_count ?? 0)}</strong></div>
              <div><span>Черновиков</span><strong>{String(selected.summary.draft_count ?? 0)}</strong></div>
              <div><span>Без категории</span><strong>{String(selected.summary.uncategorized_count ?? 0)}</strong></div>
              <div><span>Backup</span><strong>{String(selected.summary.backup_status ?? "—")}</strong></div>
            </div>
            {currencySummaries(selected.summary.currencies).length ? (
              <div className="card-list">
                {currencySummaries(selected.summary.currencies).map((item) => (
                  <div className="automation-row" key={item.currency}>
                    <strong>{item.currency}</strong>
                    <small>Доход {item.income} · расход {item.expense} · поток {item.net_cashflow}</small>
                  </div>
                ))}
              </div>
            ) : null}
            <IssueList title="Блокирующие ошибки" items={selected.blocking_issues ?? []} tone="error" />
            <IssueList title="Предупреждения" items={selected.warning_issues ?? []} tone="warning" />
            {selected.status === "ready" ? <div className="confirmation-panel"><div><span className="kicker">Явное действие</span><h2>Месяц готов</h2><p>Перед подтверждением Backend повторит все проверки и сверит версию.</p></div><button type="button" disabled={busy} onClick={() => void confirm()}>Подтвердить закрытие</button></div> : null}
            {selected.status === "confirmed" ? <div className="confirmation-panel"><div><span className="kicker">Amendment</span><h2>Открыть повторно</h2><p>Причина и смена статуса сохранятся в audit.</p></div><button className="secondary-button" type="button" disabled={busy} onClick={() => void reopen()}>Открыть месяц</button></div> : null}
          </> : <div className="empty-state">Выберите период или подготовьте новый.</div>}
        </article>
      </div>
    </section>
  );
}

function IssueList({ title, items, tone }: { title: string; items: Array<Record<string, unknown>>; tone: "error" | "warning" }) {
  return <div className={`issue-list issue-list--${tone}`}><strong>{title}: {items.length}</strong>{items.length ? <ul>{items.map((item, index) => <li key={`${String(item.code)}-${index}`}>{String(item.code)}{item.count !== undefined ? ` · ${String(item.count)}` : ""}</li>)}</ul> : <p>Нет.</p>}</div>;
}

function currencySummaries(value: unknown): Array<{ currency: string; income: string; expense: string; net_cashflow: string }> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is { currency: string; income: string; expense: string; net_cashflow: string } => (
    typeof item === "object" && item !== null
    && typeof item.currency === "string"
    && typeof item.income === "string"
    && typeof item.expense === "string"
    && typeof item.net_cashflow === "string"
  ));
}
