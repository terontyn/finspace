"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { apiClient } from "@/lib/api-client";
import type { NotificationSetting, TelegramStatus } from "@/types/automations";

const events = [
  ["weekly_report", "Недельный отчёт"],
  ["uncategorized_reminder", "Операции без категории"],
  ["recurring_due", "Регулярные операции"],
  ["month_close", "Закрытие месяца"],
  ["backup_problem", "Проблемы backup"],
  ["sync_problem", "Проблемы синхронизации"],
] as const;

export function TelegramScreen({ onError }: { onError: (error: unknown) => void }) {
  const auth = useAuth();
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [settings, setSettings] = useState<NotificationSetting[]>([]);
  const [linkCode, setLinkCode] = useState<{ code: string; expires_at: string } | null>(null);
  const [testMessage, setTestMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [link, notificationSettings] = await Promise.all([
        apiClient.get<TelegramStatus>("/api/v1/settings/telegram"),
        apiClient.get<NotificationSetting[]>("/api/v1/settings/notifications"),
      ]);
      setStatus(link);
      setSettings(notificationSettings);
    } catch (error) {
      onError(error);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  async function createCode() {
    try {
      setLinkCode(
        await apiClient.post<{ code: string; expires_at: string }>(
          "/api/v1/settings/telegram/link-code",
          {},
        ),
      );
    } catch (error) {
      onError(error);
    }
  }

  async function revoke() {
    try {
      await apiClient.delete("/api/v1/settings/telegram");
      setLinkCode(null);
      await load();
    } catch (error) {
      onError(error);
    }
  }

  async function toggle(eventType: string, enabled: boolean) {
    try {
      await apiClient.put("/api/v1/settings/notifications/telegram", {
        event_type: eventType,
        enabled,
        timezone: auth.session?.workspace.timezone ?? "Europe/Amsterdam",
      });
      await load();
    } catch (error) {
      onError(error);
    }
  }

  async function testNotification() {
    try {
      const response = await apiClient.post<{ messages: string[] }>(
        "/api/v1/settings/notifications/telegram/test",
        {},
      );
      setTestMessage(response.messages[0] ?? "Тест готов к отправке через n8n.");
    } catch (error) {
      onError(error);
    }
  }

  return (
    <section>
      <header className="screen-header"><div><span className="kicker">Локальный bot polling</span><h1>Telegram</h1><p>Bot token хранится только в credentials n8n. Username используется лишь для отображения.</p></div></header>
      <div className="two-column">
        <article className="panel">
          <div className="panel-heading"><div><span className="kicker">Привязка</span><h2>{status?.linked ? "Telegram подключён" : "Подключить Telegram"}</h2></div><span className={`sync-state ${status?.linked ? "" : "sync-state--off"}`}>{status?.linked ? "active" : "off"}</span></div>
          {status?.linked ? (
            <dl className="settings-list">
              <div><dt>Telegram ID</dt><dd>{status.telegram_user_id}</dd></div>
              <div><dt>Username</dt><dd>{status.telegram_username ? `@${status.telegram_username}` : "не указан"}</dd></div>
              <div><dt>Последняя активность</dt><dd>{formatDate(status.last_seen_at)}</dd></div>
            </dl>
          ) : (
            <ol className="setup-steps"><li>Создайте одноразовый код.</li><li>Отправьте боту <code>/link КОД</code>.</li><li>Код истекает через 10 минут.</li></ol>
          )}
          {linkCode ? <div className="link-code"><span>Одноразовый код</span><strong>{linkCode.code}</strong><small>До {formatDate(linkCode.expires_at)}</small></div> : null}
          <div className="workflow-actions">
            {!status?.linked ? <button type="button" onClick={() => void createCode()}>Создать код</button> : <button type="button" onClick={() => void testNotification()}>Проверить отправку</button>}
            {status?.linked ? <button className="secondary-button" type="button" onClick={() => void revoke()}>Отозвать привязку</button> : null}
          </div>
          {testMessage ? <div className="notice notice--success">{testMessage}</div> : null}
        </article>

        <article className="panel">
          <div className="panel-heading"><div><span className="kicker">Уведомления</span><h2>Что отправлять</h2></div></div>
          <div className="notification-list">
            {events.map(([eventType, label]) => {
              const enabled = settings.find((item) => item.event_type === eventType)?.enabled ?? false;
              return <label className="notification-row" key={eventType}><span><strong>{label}</strong><small>{eventType}</small></span><input type="checkbox" checked={enabled} disabled={!status?.linked} onChange={(event) => void toggle(eventType, event.target.checked)} /></label>;
            })}
          </div>
        </article>
      </div>
    </section>
  );
}

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString("ru-RU") : "—";
}
