"use client";

import { useCallback, useEffect, useState } from "react";

import { apiClient } from "@/lib/api-client";
import type {
  ApiPage,
  AutomationRun,
  AutomationStatus,
  ServiceAccount,
} from "@/types/automations";

export function AutomationsScreen({ onError }: { onError: (error: unknown) => void }) {
  const [status, setStatus] = useState<AutomationStatus | null>(null);
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [runs, setRuns] = useState<AutomationRun[]>([]);
  const [name, setName] = useState("Локальный n8n");
  const [oneTimeKey, setOneTimeKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [statusResult, accountResult, runResult] = await Promise.all([
        apiClient.get<AutomationStatus>("/api/v1/automation/status"),
        apiClient.get<ApiPage<ServiceAccount>>("/api/v1/settings/service-accounts"),
        apiClient.get<ApiPage<AutomationRun>>("/api/v1/automation/runs?limit=20"),
      ]);
      setStatus(statusResult);
      setAccounts(accountResult.items);
      setRuns(runResult.items);
    } catch (error) {
      onError(error);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  async function createServiceAccount() {
    try {
      const created = await apiClient.post<{ service_account: ServiceAccount }>(
        "/api/v1/settings/service-accounts",
        { name, service_type: "n8n", permissions: [] },
      );
      const key = await apiClient.post<{ key: string }>(
        `/api/v1/settings/service-accounts/${created.service_account.id}/keys`,
        {},
      );
      setOneTimeKey(key.key);
      await load();
    } catch (error) {
      onError(error);
    }
  }

  async function rotateKey(account: ServiceAccount) {
    try {
      const result = await apiClient.post<{ key: string }>(
        `/api/v1/settings/service-accounts/${account.id}/rotate-key`,
        {},
      );
      setOneTimeKey(result.key);
      await load();
    } catch (error) {
      onError(error);
    }
  }

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Локальные интеграции</span>
          <h1>Автоматизации</h1>
          <p>n8n вызывает только ограниченный Backend API; финансовая логика остаётся здесь.</p>
        </div>
        <a
          className="secondary-button link-button"
          href="http://localhost:5678"
          target="_blank"
          rel="noreferrer"
        >
          Открыть n8n
        </a>
      </header>

      <div className="metric-grid">
        <article className="metric-card">
          <span>Состояние n8n</span>
          <strong>{loading ? "…" : statusLabel(status?.status)}</strong>
          <small>Heartbeat: {formatDate(status?.last_heartbeat_at)}</small>
        </article>
        <article className="metric-card">
          <span>Service account</span>
          <strong>{status?.active_service_account?.status ?? "не создан"}</strong>
          <small>Секреты после выпуска не отображаются.</small>
        </article>
        <article className="metric-card">
          <span>Последний сбой</span>
          <strong>{status?.recent_failed_run?.error_code ?? "нет"}</strong>
          <small>{formatDate(status?.recent_failed_run?.finished_at)}</small>
        </article>
      </div>

      {oneTimeKey ? (
        <div className="panel secret-panel">
          <span className="kicker">Показывается один раз</span>
          <h2>Сохраните ServiceKey в credentials n8n</h2>
          <code>{oneTimeKey}</code>
          <button className="secondary-button" type="button" onClick={() => setOneTimeKey(null)}>
            Я сохранил ключ
          </button>
        </div>
      ) : null}

      <div className="two-column automation-grid">
        <article className="panel">
          <div className="panel-heading">
            <div><span className="kicker">Доступ</span><h2>Service accounts</h2></div>
          </div>
          <div className="inline-form">
            <input value={name} onChange={(event) => setName(event.target.value)} aria-label="Имя service account" />
            <button type="button" className="secondary-button" onClick={() => void createServiceAccount()}>
              Создать и выпустить ключ
            </button>
          </div>
          <div className="card-list">
            {accounts.map((account) => (
              <div className="automation-row" key={account.id}>
                <div><strong>{account.name}</strong><small>{account.status} · {account.keys.length} ключ.</small></div>
                <button className="text-button" type="button" onClick={() => void rotateKey(account)} disabled={account.status !== "active"}>
                  Повернуть ключ
                </button>
              </div>
            ))}
          </div>
        </article>

        <article className="panel">
          <div className="panel-heading"><div><span className="kicker">Журнал</span><h2>Последние запуски</h2></div></div>
          <div className="card-list">
            {runs.length ? runs.map((run) => (
              <div className="automation-row" key={run.id}>
                <div><strong>{run.automation_type}</strong><small>{formatDate(run.started_at)}</small></div>
                <span className={`status-chip status-chip--${run.status}`}>{run.status}</span>
              </div>
            )) : <div className="empty-state">Запусков пока нет.</div>}
          </div>
          <span className="text-button docs-link">Документация: docs/n8n.md</span>
        </article>
      </div>
    </section>
  );
}

function statusLabel(value?: AutomationStatus["status"]) {
  if (value === "healthy") return "работает";
  if (value === "stale") return "нет heartbeat";
  return "не настроен";
}

function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString("ru-RU") : "—";
}
