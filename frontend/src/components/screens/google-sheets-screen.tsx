"use client";

import { useCallback, useEffect, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { fullExportMessage, googleStateLabel } from "@/lib/google-sync";
import type {
  AppsScriptBindingSecret,
  AppsScriptPackage,
  FullExportPreview,
  GoogleConnectResponse,
  GoogleSheetStatus,
} from "@/types/google";

interface Props {
  onError: (error: unknown) => void;
}

function dateLabel(value: string | null): string {
  return value ? new Date(value).toLocaleString("ru-RU") : "ещё не выполнялось";
}

export function GoogleSheetsScreen({ onError }: Props) {
  const [status, setStatus] = useState<GoogleSheetStatus | null>(null);
  const [secret, setSecret] = useState<AppsScriptBindingSecret | null>(null);
  const [sourcePackage, setSourcePackage] = useState<AppsScriptPackage | null>(null);
  const [selectedFile, setSelectedFile] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await apiClient.get<GoogleSheetStatus>("/api/v1/google-sheets/status"));
    } catch (error) {
      onError(error);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  async function action(work: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    try {
      await work();
      await load();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }

  async function connectOAuth(): Promise<void> {
    await action(async () => {
      const result = await apiClient.post<GoogleConnectResponse>(
        "/api/v1/integrations/google/connect",
      );
      window.location.assign(result.authorization_url);
    });
  }

  async function fullExport(): Promise<void> {
    await action(async () => {
      const preview = await apiClient.get<FullExportPreview>(
        "/api/v1/google-sheets/full-export-preview",
      );
      if (!window.confirm(fullExportMessage(preview))) return;
      await apiClient.post("/api/v1/google-sheets/full-export", { force: preview.blocked });
    });
  }

  async function createBridgeBinding(): Promise<void> {
    await action(async () => {
      setSecret(
        await apiClient.post<AppsScriptBindingSecret>(
          "/api/v1/google-sheets/apps-script/binding",
        ),
      );
    });
  }

  async function rotateBridgeSecret(): Promise<void> {
    if (!window.confirm("Предыдущий secret немедленно перестанет действовать. Продолжить?")) return;
    await action(async () => {
      setSecret(
        await apiClient.post<AppsScriptBindingSecret>(
          "/api/v1/google-sheets/apps-script/binding/rotate-secret",
          { rebind: false },
        ),
      );
    });
  }

  async function loadPackage(): Promise<void> {
    await action(async () => {
      const result = await apiClient.get<AppsScriptPackage>(
        "/api/v1/google-sheets/apps-script/package",
      );
      setSourcePackage(result);
      setSelectedFile(Object.keys(result.files).sort()[0] ?? "");
    });
  }

  async function copyText(value: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
    } catch (error) {
      onError(error);
    }
  }

  function downloadPackage(): void {
    if (!sourcePackage) return;
    const blob = new Blob([JSON.stringify(sourcePackage.files, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "finspace-apps-script-v1.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  if (!status) return <div className="panel">Загружаем состояние Google Sheets…</div>;

  const bridge = status.provider === "apps_script_bridge";
  const connection = status.connection;
  const bindingId = secret?.id ?? status.binding_id;
  const backendUrl = secret?.backend_url ?? status.public_backend_url;

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Настройки → интеграции</span>
          <h1>Google Sheets</h1>
          <p>Основной провайдер — Apps Script Bridge. Google Cloud OAuth не обязателен.</p>
        </div>
        <span className={`sync-state sync-state--${status.status ?? "off"}`}>
          {googleStateLabel(status)}
        </span>
      </header>

      {!status.configured ? (
        <div className="notice notice--error">
          {bridge
            ? "Apps Script Bridge не готов: проверьте APPS_SCRIPT_BRIDGE_ENABLED, PUBLIC_BACKEND_URL и Redis."
            : "OAuth-провайдер не настроен."}
        </div>
      ) : null}

      <div className="metric-grid sync-metrics">
        <article className="metric-card"><span>Outbox ожидает</span><strong>{status.pending_outbox}</strong></article>
        <article className="metric-card"><span>Ошибки доставки</span><strong>{status.failed_events}</strong></article>
        <article className="metric-card"><span>Конфликты</span><strong>{status.open_conflicts}</strong></article>
      </div>

      {bridge ? (
        <>
          <div className="two-column">
            <article className="panel">
              <div className="panel-heading">
                <div><span className="kicker">Без Google Cloud Console</span><h2>Подключение Apps Script Bridge</h2></div>
              </div>
              <ol className="setup-steps">
                <li>Создайте binding и сразу сохраните показанный secret.</li>
                <li>Получите исходники Apps Script ниже.</li>
                <li><a href="https://docs.google.com/spreadsheets/u/0/create" target="_blank" rel="noreferrer">Создайте пустую Google-таблицу</a>.</li>
                <li>Откройте «Расширения → Apps Script» и создайте файлы из пакета.</li>
                <li>Запустите <code>setupFinspace()</code> — установится шаблон v1.</li>
                <li>Запустите <code>configureConnection()</code> и введите URL, binding ID и secret.</li>
                <li>В меню книги установите триггеры.</li>
                <li>Проверьте heartbeat в статусе ниже.</li>
                <li>Нажмите в книге «Получить обновления» для начального экспорта.</li>
              </ol>
              <div className="workflow-actions sync-actions">
                {!status.binding_id ? (
                  <button disabled={busy || !status.configured} onClick={() => void createBridgeBinding()} type="button">
                    Создать binding
                  </button>
                ) : null}
                <button className="secondary-button" disabled={busy} onClick={() => void loadPackage()} type="button">
                  Получить Apps Script
                </button>
                <a className="secondary-button link-button" href="https://docs.google.com/spreadsheets/u/0/create" target="_blank" rel="noreferrer">
                  Новая Google-таблица
                </a>
                {status.spreadsheet_url ? (
                  <a className="secondary-button link-button" href={status.spreadsheet_url} target="_blank" rel="noreferrer">
                    Открыть связанную таблицу
                  </a>
                ) : null}
              </div>
            </article>

            <article className="panel">
              <div className="panel-heading"><div><span className="kicker">Состояние</span><h2>Bridge</h2></div></div>
              <dl className="settings-list">
                <div><dt>Provider</dt><dd>{status.provider}</dd></div>
                <div><dt>Binding ID</dt><dd>{bindingId ?? "не создан"}</dd></div>
                <div><dt>Backend URL</dt><dd>{backendUrl ?? "не настроен"}</dd></div>
                <div><dt>Таблица</dt><dd>{status.spreadsheet_registered ? "зарегистрирована" : "ожидает регистрации"}</dd></div>
                <div><dt>Heartbeat</dt><dd>{status.heartbeat_healthy ? "активен" : "не получен или устарел"}</dd></div>
                <div><dt>Последний heartbeat</dt><dd>{dateLabel(status.last_heartbeat_at)}</dd></div>
                <div><dt>Последний pull</dt><dd>{dateLabel(status.last_pull_at)}</dd></div>
                <div><dt>Последний ACK</dt><dd>{dateLabel(status.last_ack_at)}</dd></div>
              </dl>
              <div className="control-stack">
                <button className="secondary-button" disabled={busy || !status.binding_id} onClick={() => void action(() => apiClient.post(status.status === "paused" ? "/api/v1/google-sheets/apps-script/binding/resume" : "/api/v1/google-sheets/apps-script/binding/pause"))} type="button">
                  {status.status === "paused" ? "Возобновить" : "Приостановить"}
                </button>
                <button className="secondary-button" disabled={busy || !status.binding_id} onClick={() => void rotateBridgeSecret()} type="button">Повернуть secret</button>
                <button className="text-button text-button--danger" disabled={busy || !status.binding_id} onClick={() => {
                  if (window.confirm("Удалить binding? Финансовые данные PostgreSQL останутся.")) {
                    void action(() => apiClient.delete("/api/v1/google-sheets/apps-script/binding"));
                  }
                }} type="button">Удалить binding</button>
              </div>
            </article>
          </div>

          {secret ? (
            <article className="panel secret-panel">
              <span className="kicker">Показывается только сейчас</span>
              <h2>Данные подключения</h2>
              <p>{secret.warning}</p>
              <div className="secret-fields">
                <div><strong>Backend URL</strong><code>{secret.backend_url}</code><button className="secondary-button" onClick={() => void copyText(secret.backend_url)} type="button">Копировать</button></div>
                <div><strong>Binding ID</strong><code>{secret.id}</code><button className="secondary-button" onClick={() => void copyText(secret.id)} type="button">Копировать</button></div>
                <div><strong>Secret</strong><code>{secret.secret}</code><button className="secondary-button" onClick={() => void copyText(secret.secret)} type="button">Копировать</button></div>
              </div>
              <button className="secondary-button" onClick={() => setSecret(null)} type="button">Скрыть secret навсегда</button>
            </article>
          ) : null}

          {sourcePackage ? (
            <article className="panel package-panel">
              <div className="panel-heading">
                <div><span className="kicker">Apps Script v1</span><h2>Исходные файлы</h2></div>
                <button className="secondary-button" onClick={downloadPackage} type="button">Скачать пакет JSON</button>
              </div>
              <div className="package-tabs">
                {Object.keys(sourcePackage.files).sort().map((name) => (
                  <button className={selectedFile === name ? "package-tab package-tab--active" : "package-tab"} key={name} onClick={() => setSelectedFile(name)} type="button">{name}</button>
                ))}
              </div>
              {selectedFile ? (
                <>
                  <pre className="source-code">{sourcePackage.files[selectedFile]}</pre>
                  <button className="secondary-button" onClick={() => void copyText(sourcePackage.files[selectedFile])} type="button">Копировать {selectedFile}</button>
                </>
              ) : null}
            </article>
          ) : null}
        </>
      ) : null}

      {status.oauth_enabled ? (
        <article className="panel oauth-panel">
          <div className="panel-heading"><div><span className="kicker">Необязательный provider</span><h2>Google OAuth / Sheets API</h2></div></div>
          <p>Этот прежний способ доступен только потому, что GOOGLE_OAUTH_ENABLED=true.</p>
          <dl className="settings-list">
            <div><dt>Состояние</dt><dd>{connection.status ?? "не подключён"}</dd></div>
            <div><dt>Email</dt><dd>{connection.google_email ?? "—"}</dd></div>
            <div><dt>Scopes</dt><dd>{connection.granted_scopes.join(", ") || "—"}</dd></div>
          </dl>
          <div className="workflow-actions sync-actions">
            {!connection.connected ? <button disabled={busy || !connection.configured} onClick={() => void connectOAuth()} type="button">Подключить Google</button> : null}
            {connection.connected && !status.binding_id ? <button disabled={busy} onClick={() => void action(() => apiClient.post("/api/v1/google-sheets/create"))} type="button">Создать основную книгу</button> : null}
            <button className="secondary-button" disabled={busy || !status.binding_id} onClick={() => void action(() => apiClient.post("/api/v1/google-sheets/reconcile"))} type="button">Полная сверка</button>
            <button className="secondary-button" disabled={busy || !status.binding_id} onClick={() => void fullExport()} type="button">Повторный full export</button>
          </div>
        </article>
      ) : null}
    </section>
  );
}
