"use client";

import { redirect } from "next/navigation";
import { useCallback, useState } from "react";

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

/* SVG-иконки для навигации */
const NavIcons: Record<Screen, React.ReactNode> = {
  today: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>
    </svg>
  ),
  transactions: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>
    </svg>
  ),
  accounts: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/>
    </svg>
  ),
  categories: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 6h16M4 12h10M4 18h6"/>
    </svg>
  ),
  imports: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
    </svg>
  ),
  google: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 3h6v6H3zM15 3h6v6h-6zM15 15h6v6h-6zM3 15h6v6H3z"/>
    </svg>
  ),
  conflicts: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  ),
  automations: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3"/><path d="M19.07 4.93A10 10 0 0 0 4.93 19.07M4.93 4.93a10 10 0 0 1 14.14 14.14"/>
    </svg>
  ),
  recurring: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/>
      <path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
    </svg>
  ),
  telegram: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
    </svg>
  ),
  "month-close": (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>
      <polyline points="9 16 11 18 15 14"/>
    </svg>
  ),
};

const navigation: Array<{ id: Screen; label: string }> = [
  { id: "today",       label: "Сегодня" },
  { id: "transactions",label: "Операции" },
  { id: "accounts",    label: "Счета" },
  { id: "categories",  label: "Категории" },
  { id: "imports",     label: "Импорт" },
  { id: "google",      label: "Google Sheets" },
  { id: "conflicts",   label: "Конфликты" },
  { id: "automations", label: "Автоматизации" },
  { id: "recurring",   label: "Регулярные" },
  { id: "telegram",    label: "Telegram" },
  { id: "month-close", label: "Закрытие" },
];

/* Иконка выхода */
function LogoutIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
    </svg>
  );
}

export function FinanceApp() {
  const auth = useAuth();
  const [screen, setScreen] = useState<Screen>("today");
  const [error, setError] = useState<string | null>(null);

  const showError = useCallback((requestError: unknown) => {
    if (requestError instanceof ApiClientError) {
      const requestHint = requestError.requestId ? ` · запрос ${requestError.requestId}` : "";
      setError(`${requestError.message}${requestHint}`);
      return;
    }
    setError("Произошла непредвиденная ошибка.");
  }, []);

  if (auth.loading) {
    return (
      <main className="loading-page">
        <div className="loading-content">
          <div className="loading-spinner" />
          <span>Восстанавливаем сессию…</span>
        </div>
      </main>
    );
  }

  if (!auth.session) {
    redirect("/login");
  }

  const initials = auth.session.user.display_name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  async function handleLogout() {
    try {
      await auth.logout();
      window.location.replace("/login");
    } catch {
      window.location.replace("/login");
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        {/* Бренд */}
        <div className="brand">
          <div className="brand-logo">Ф</div>
          <div className="brand-text">
            <strong className="brand-name">Финпространство</strong>
            <span className="brand-workspace">{auth.session.workspace.name}</span>
          </div>
        </div>

        {/* Навигация */}
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
              title={item.label}
            >
              <span className="nav-icon" aria-hidden="true">
                {NavIcons[item.id]}
              </span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>

        {/* Сессия */}
        <div className="sidebar-session">
          <div className="sidebar-user">
            <div className="sidebar-avatar" title={auth.session.user.display_name}>
              {initials}
            </div>
            <div className="sidebar-user-info">
              <span className="sidebar-user-name">{auth.session.user.display_name}</span>
              <span className="sidebar-user-email">{auth.session.user.email}</span>
            </div>
            <button
              className="sidebar-logout"
              type="button"
              onClick={() => void handleLogout()}
              title="Выйти из аккаунта"
            >
              <LogoutIcon />
            </button>
          </div>
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

        {screen === "today"        && <TodayScreen onError={showError} />}
        {screen === "transactions" && <TransactionsScreen onError={showError} />}
        {screen === "accounts"     && <AccountsScreen onError={showError} />}
        {screen === "categories"   && <CategoriesScreen onError={showError} />}
        {screen === "imports"      && <ImportScreen onError={showError} />}
        {screen === "google"       && <GoogleSheetsScreen onError={showError} />}
        {screen === "conflicts"    && <SyncConflictsScreen onError={showError} />}
        {screen === "automations"  && <AutomationsScreen onError={showError} />}
        {screen === "recurring"    && <RecurringRulesScreen onError={showError} />}
        {screen === "telegram"     && <TelegramScreen onError={showError} />}
        {screen === "month-close"  && <MonthCloseScreen onError={showError} />}
      </main>
    </div>
  );
}
