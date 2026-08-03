"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { AccountsScreen } from "@/components/screens/accounts-screen";
import { AutomationsScreen } from "@/components/screens/automations-screen";
import { CategoriesScreen } from "@/components/screens/categories-screen";
import { ImportScreen } from "@/components/screens/import-screen";
import { MonthCloseScreen } from "@/components/screens/month-close-screen";
import { RecurringRulesScreen } from "@/components/screens/recurring-rules-screen";
import { GoogleSheetsScreen } from "@/components/screens/google-sheets-screen";
import { SyncConflictsScreen } from "@/components/screens/sync-conflicts-screen";
import { TodayScreen } from "@/components/screens/today-screen";
import { TelegramScreen } from "@/components/screens/telegram-screen";
import { TransactionsScreen } from "@/components/screens/transactions-screen";
import { ApiClientError } from "@/lib/api-client";

type Screen =
  | "today"
  | "transactions"
  | "accounts"
  | "categories"
  | "imports"
  | "google"
  | "conflicts"
  | "automations"
  | "recurring"
  | "telegram"
  | "month-close";

const navigation: Array<{ id: Screen; label: string; marker: string }> = [
  { id: "today", label: "Сегодня", marker: "⌁" },
  { id: "transactions", label: "Операции", marker: "↕" },
  { id: "accounts", label: "Счета", marker: "▣" },
  { id: "categories", label: "Категории", marker: "◫" },
  { id: "imports", label: "Импорт", marker: "⇣" },
  { id: "google", label: "Google Sheets", marker: "G" },
  { id: "conflicts", label: "Конфликты", marker: "!" },
  { id: "automations", label: "Автоматизации", marker: "⚙" },
  { id: "recurring", label: "Регулярные", marker: "↻" },
  { id: "telegram", label: "Telegram", marker: "✈" },
  { id: "month-close", label: "Закрытие месяца", marker: "✓" },
];

export function FinanceApp() {
  const auth = useAuth();
  const [screen, setScreen] = useState<Screen>("today");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!auth.loading && !auth.session) window.location.replace("/login");
  }, [auth.loading, auth.session]);

  const showError = useCallback((requestError: unknown) => {
    if (requestError instanceof ApiClientError) {
      const requestHint = requestError.requestId ? ` · request ${requestError.requestId}` : "";
      setError(`${requestError.message}${requestHint}`);
      return;
    }
    setError("Произошла непредвиденная ошибка.");
  }, []);

  if (auth.loading || !auth.session) {
    return <main className="loading-page">Восстанавливаем защищённую сессию…</main>;
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-symbol">Ф</div>
          <div>
            <strong>Финпространство</strong>
            <span>{auth.session.workspace.name}</span>
          </div>
        </div>
        <nav aria-label="Основная навигация">
          {navigation.map((item) => (
            <button
              className={screen === item.id ? "nav-item nav-item--active" : "nav-item"}
              key={item.id}
              onClick={() => {
                setScreen(item.id);
                setError(null);
              }}
              type="button"
            >
              <span aria-hidden="true">{item.marker}</span>
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-session">
          <span>{auth.session.user.display_name}</span>
          <small>{auth.session.user.email}</small>
          <button
            className="text-button"
            type="button"
            onClick={() => void auth.logout().then(() => window.location.replace("/login"))}
          >
            Выйти
          </button>
        </div>
      </aside>

      <main className="workspace">
        {error ? (
          <div className="notice notice--error" role="alert">
            <span>{error}</span>
            <button type="button" className="text-button" onClick={() => setError(null)}>
              Закрыть
            </button>
          </div>
        ) : null}
        {screen === "today" ? <TodayScreen onError={showError} /> : null}
        {screen === "transactions" ? <TransactionsScreen onError={showError} /> : null}
        {screen === "accounts" ? <AccountsScreen onError={showError} /> : null}
        {screen === "categories" ? <CategoriesScreen onError={showError} /> : null}
        {screen === "imports" ? <ImportScreen onError={showError} /> : null}
        {screen === "google" ? <GoogleSheetsScreen onError={showError} /> : null}
        {screen === "conflicts" ? <SyncConflictsScreen onError={showError} /> : null}
        {screen === "automations" ? <AutomationsScreen onError={showError} /> : null}
        {screen === "recurring" ? <RecurringRulesScreen onError={showError} /> : null}
        {screen === "telegram" ? <TelegramScreen onError={showError} /> : null}
        {screen === "month-close" ? <MonthCloseScreen onError={showError} /> : null}
      </main>
    </div>
  );
}
