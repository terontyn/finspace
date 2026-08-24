"use client";

import Link from "next/link";
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

function connectionLabel(status: GoogleSheetStatus): string {
  if (!status.configured || !status.binding_id) return "Не подключено";
  if (status.status === "error" || status.failed_events > 0) return "Ошибка";
  if (status.status === "paused") return "Приостановлено";
  if (!status.spreadsheet_registered) return "Ожидает настройки";
  if (status.provider === "apps_script_bridge" && !status.heartbeat_healthy) return "Нет связи";
  return "Подключено";
}

export function GoogleSheetsScreen({ onError }: Props) {
  const [status, setStatus] = useState<GoogleSheetStatus | null>(null);
  const [secret, setSecret] = useState<AppsScriptBindingSecret | null>(null);
  const [sourcePackage, setSourcePackage] = useState<AppsScriptPackage | null>(null);
  const [selectedFile, setSelectedFile] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setStatus(await apiClient.get<GoogleSheetStatus>("/api/v1/google-sheets/status"));
    } catch (error) {
      onError(error);
    } finally {
      setLoading(false);
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
      setSecret(await apiClient.post<AppsScriptBindingSecret>(
        "/api/v1/google-sheets/apps-script/binding",
      ));
    });
  }

  async function rotateBridgeSecret(rebind: boolean): Promise<void> {
    const warning = rebind
      ? "Текущая таблица будет отвязана. Потребуется заново настроить Apps Script. Продолжить?"
      : "Предыдущий secret немедленно перестанет действовать. Продолжить?";
    if (!window.confirm(warning)) return;
    await action(async () => {
      setSecret(await apiClient.post<AppsScriptBindingSecret>(
        "/api/v1/google-sheets/apps-script/binding/rotate-secret",
        { rebind },
      ));
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

  if (loading) return <div className="panel">Загружаем состояние Google Sheets…</div>;
  if (!status) return <div className="panel empty-state"><strong>Статус недоступен</strong><button type="button" onClick={() => void load()}>Повторить</button></div>;

  const bridge = status.provider === "apps_script_bridge";
  const connected = Boolean(status.binding_id && status.spreadsheet_registered);
  const bindingId = secret?.id ?? status.binding_id;
  const backendUrl = secret?.backend_url ?? status.public_backend_url;
  const lastActivity = status.last_successful_sync_at ?? status.last_ack_at ?? status.last_pull_at;

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Настройки → интеграции</span>
          <h1>Google Sheets</h1>
          <p>Рабочая книга синхронизируется с PostgreSQL через защищённый Apps Script Bridge.</p>
        </div>
        <span className={`sync-state sync-state--${status.status ?? "off"}`}>{connectionLabel(status)}</span>
      </header>

      {!status.configured ? (
        <div className="notice notice--error">
          {bridge
            ? "Apps Script Bridge не готов. Администратору нужно проверить публичный HTTPS URL и Redis."
            : "Google OAuth не настроен администратором."}
        </div>
      ) : null}

      <div className="google-product-grid">
        <article className="panel google-connection-card">
          <div className="panel-heading">
            <div><span className="kicker">Подключение</span><h2>{status.spreadsheet_name ?? "Основная Google-таблица"}</h2></div>
            <span className={`status-chip ${connected ? "status-chip--valid" : "status-chip--duplicate"}`}>{connectionLabel(status)}</span>
          </div>
          <dl className="settings-list">
            <div><dt>Состояние</dt><dd>{googleStateLabel(status)}</dd></div>
            <div><dt>Последняя успешная синхронизация</dt><dd>{dateLabel(lastActivity)}</dd></div>
            <div><dt>Неразрешённые конфликты</dt><dd>{status.open_conflicts}</dd></div>
            {status.last_error_code || status.last_error_message ? <div><dt>Последняя ошибка</dt><dd>{status.last_error_message ?? status.last_error_code}</dd></div> : null}
          </dl>
          <div className="workflow-actions sync-actions">
            <button className="secondary-button" disabled={busy} onClick={() => void load()} type="button">Обновить состояние</button>
            {status.spreadsheet_url ? <a className="secondary-button link-button" href={status.spreadsheet_url} target="_blank" rel="noreferrer">Открыть таблицу</a> : null}
            {status.open_conflicts ? <Link className="secondary-button link-button" href="/conflicts">Разрешить конфликты</Link> : null}
          </div>
          {bridge && connected ? <p className="section-copy">Чтобы запустить обмен немедленно, откройте книгу и выберите «Финпространство → Отправить изменения» или «Получить обновления». Автоматический trigger работает каждые 5 минут.</p> : null}
        </article>

        <article className="panel google-sync-summary">
          <div className="panel-heading"><div><span className="kicker">Синхронизация</span><h2>Текущее состояние</h2></div></div>
          <div className="google-summary-metrics">
            <div><span>К отправке</span><strong>{status.pending_outbox}</strong></div>
            <div><span>В обработке</span><strong>{status.pending_inbox}</strong></div>
            <div><span>Ошибки</span><strong>{status.failed_events}</strong></div>
            <div><span>Конфликты</span><strong>{status.open_conflicts}</strong></div>
          </div>
          <p className="section-copy">Finspace остаётся источником истины. Изменения из таблицы проходят inbox, валидацию и optimistic locking.</p>
        </article>
      </div>

      {bridge && !status.binding_id ? (
        <article className="panel google-setup-card">
          <div className="panel-heading"><div><span className="kicker">Первое подключение</span><h2>Подключить Google-таблицу</h2></div></div>
          <ol className="setup-steps">
            <li>Создайте подключение и сохраните одноразовый secret.</li>
            <li>Создайте пустую Google-таблицу и откройте «Расширения → Apps Script».</li>
            <li>Добавьте файлы из пакета и запустите <code>setupFinspace()</code>.</li>
            <li>Вернитесь в таблицу, обновите страницу и выберите «Финпространство → Настроить подключение».</li>
            <li>Установите триггеры из меню «Финпространство».</li>
          </ol>
          <div className="workflow-actions sync-actions">
            <button disabled={busy || !status.configured} onClick={() => void createBridgeBinding()} type="button">Создать подключение</button>
            <a className="secondary-button link-button" href="https://docs.google.com/spreadsheets/u/0/create" target="_blank" rel="noreferrer">Новая Google-таблица</a>
          </div>
        </article>
      ) : null}

      {secret ? (
        <article className="panel secret-panel">
          <span className="kicker">Показывается только сейчас</span>
          <h2>Данные подключения</h2>
          <p>{secret.warning} Не сохраняйте secret в ячейках книги, сообщениях или скриншотах.</p>
          <div className="secret-fields">
            <div><strong>Backend URL</strong><code>{secret.backend_url}</code><button className="secondary-button" onClick={() => void copyText(secret.backend_url)} type="button">Копировать</button></div>
            <div><strong>Binding ID</strong><code>{secret.id}</code><button className="secondary-button" onClick={() => void copyText(secret.id)} type="button">Копировать</button></div>
            <div><strong>Secret</strong><code>{secret.secret}</code><button className="secondary-button" onClick={() => void copyText(secret.secret)} type="button">Копировать</button></div>
          </div>
          <button className="secondary-button" onClick={() => setSecret(null)} type="button">Скрыть secret навсегда</button>
        </article>
      ) : null}

      {bridge && status.binding_id ? (
        <details className="panel google-settings-panel">
          <summary><span><strong>Настройки подключения</strong><small>Apps Script, пауза, переподключение</small></span></summary>
          <ol className="setup-steps">
            <li>Получите пакет Apps Script и перенесите файлы в связанную книгу.</li>
            <li>Для первичной настройки запустите <code>setupFinspace()</code>, затем обновите таблицу и выберите «Финпространство → Настроить подключение».</li>
            <li>Secret доступен только после создания или ротации.</li>
          </ol>
          <div className="workflow-actions sync-actions">
            <button className="secondary-button" disabled={busy} onClick={() => void loadPackage()} type="button">Получить Apps Script</button>
            <button className="secondary-button" disabled={busy} onClick={() => void action(() => apiClient.post(status.status === "paused" ? "/api/v1/google-sheets/apps-script/binding/resume" : "/api/v1/google-sheets/apps-script/binding/pause"))} type="button">{status.status === "paused" ? "Возобновить" : "Приостановить"}</button>
            <button className="secondary-button" disabled={busy} onClick={() => void rotateBridgeSecret(false)} type="button">Обновить secret</button>
            <button className="secondary-button" disabled={busy} onClick={() => void rotateBridgeSecret(true)} type="button">Переподключить таблицу</button>
            <button className="text-button text-button--danger" disabled={busy} onClick={() => { if (window.confirm("Удалить подключение? Финансовые данные PostgreSQL останутся.")) { void action(() => apiClient.delete("/api/v1/google-sheets/apps-script/binding")); } }} type="button">Удалить подключение</button>
          </div>
        </details>
      ) : null}

      {sourcePackage ? (
        <article className="panel package-panel">
          <div className="panel-heading"><div><span className="kicker">Apps Script v1</span><h2>Исходные файлы</h2></div><button className="secondary-button" onClick={downloadPackage} type="button">Скачать пакет JSON</button></div>
          <div className="package-tabs">{Object.keys(sourcePackage.files).sort().map((name) => <button className={selectedFile === name ? "package-tab package-tab--active" : "package-tab"} key={name} onClick={() => setSelectedFile(name)} type="button">{name}</button>)}</div>
          {selectedFile ? <><pre className="source-code">{sourcePackage.files[selectedFile]}</pre><button className="secondary-button" onClick={() => void copyText(sourcePackage.files[selectedFile])} type="button">Копировать {selectedFile}</button></> : null}
        </article>
      ) : null}

      <details className="panel google-diagnostics">
        <summary><span><strong>Диагностика</strong><small>Техническое состояние Bridge без секретов</small></span></summary>
        <dl className="settings-list">
          <div><dt>Provider</dt><dd>{status.provider}</dd></div>
          <div><dt>Binding ID</dt><dd>{bindingId ?? "не создан"}</dd></div>
          <div><dt>Workbook ID</dt><dd>{status.spreadsheet_id ?? "не зарегистрирован"}</dd></div>
          <div><dt>Backend URL</dt><dd>{backendUrl ?? "не настроен"}</dd></div>
          <div><dt>Heartbeat</dt><dd>{status.heartbeat_healthy ? "активен" : "не получен или устарел"}</dd></div>
          <div><dt>Последний heartbeat</dt><dd>{dateLabel(status.last_heartbeat_at)}</dd></div>
          <div><dt>Последний pull</dt><dd>{dateLabel(status.last_pull_at)}</dd></div>
          <div><dt>Последний ACK</dt><dd>{dateLabel(status.last_ack_at)}</dd></div>
          <div><dt>Последняя сверка</dt><dd>{dateLabel(status.last_reconciliation_at)}</dd></div>
          <div><dt>Outbox / Inbox</dt><dd>{status.pending_outbox} / {status.pending_inbox}</dd></div>
          <div><dt>Ошибка</dt><dd>{status.last_error_code ?? "нет"}</dd></div>
        </dl>
      </details>

      {status.provider === "google_oauth" && status.oauth_enabled ? (
        <details className="panel oauth-panel">
          <summary><span><strong>Google OAuth / Sheets API</strong><small>Необязательный legacy provider</small></span></summary>
          <dl className="settings-list"><div><dt>Состояние</dt><dd>{status.connection.status ?? "не подключён"}</dd></div><div><dt>Email</dt><dd>{status.connection.google_email ?? "—"}</dd></div></dl>
          <div className="workflow-actions sync-actions">
            {!status.connection.connected ? <button disabled={busy || !status.connection.configured} onClick={() => void connectOAuth()} type="button">Подключить Google</button> : null}
            {status.connection.connected && !status.binding_id ? <button disabled={busy} onClick={() => void action(() => apiClient.post("/api/v1/google-sheets/create"))} type="button">Создать основную книгу</button> : null}
            <button className="secondary-button" disabled={busy || !status.binding_id} onClick={() => void action(() => apiClient.post("/api/v1/google-sheets/reconcile"))} type="button">Полная сверка</button>
            <button className="secondary-button" disabled={busy || !status.binding_id} onClick={() => void fullExport()} type="button">Повторный full export</button>
          </div>
        </details>
      ) : null}
    </section>
  );
}
