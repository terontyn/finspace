"use client";

import { redirect } from "next/navigation";
import { useCallback, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { AccountDetailsScreen } from "@/components/screens/account-details-screen";
import { AccountsScreen } from "@/components/screens/accounts-screen";
import { AutomationsScreen } from "@/components/screens/automations-screen";
import { BudgetScreen } from "@/components/screens/budget-screen";
import { CategoriesScreen } from "@/components/screens/categories-screen";
import { ComingSoonScreen } from "@/components/screens/coming-soon-screen";
import { GoogleSheetsScreen } from "@/components/screens/google-sheets-screen";
import { GoalsScreen } from "@/components/screens/goals-screen";
import { ImportScreen } from "@/components/screens/import-screen";
import { MonthCloseScreen } from "@/components/screens/month-close-screen";
import { PayeesScreen } from "@/components/screens/payees-screen";
import { RecurringRulesScreen } from "@/components/screens/recurring-rules-screen";
import { ReportsScreen } from "@/components/screens/reports-screen";
import { SyncConflictsScreen } from "@/components/screens/sync-conflicts-screen";
import { TelegramScreen } from "@/components/screens/telegram-screen";
import { TodayScreen } from "@/components/screens/today-screen";
import { TransactionsScreen } from "@/components/screens/transactions-screen";
import { AppShell } from "@/components/shell/app-shell";
import type { AppScreen } from "@/components/shell/navigation";
import { ApiClientError } from "@/lib/api-client";

interface FinanceAppProps {
  accountId?: string;
  initialTransactionAccountId?: string;
  openTransactionForm?: boolean;
  screen: AppScreen;
}

export function FinanceApp({ accountId, initialTransactionAccountId, openTransactionForm = false, screen }: FinanceAppProps) {
  const auth = useAuth();
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
    return <main className="loading-page"><div className="loading-content"><div className="loading-spinner"/><span>Восстанавливаем защищённую сессию…</span></div></main>;
  }
  if (!auth.session) redirect("/login");

  async function handleLogout() {
    try { await auth.logout(); } finally { window.location.replace("/login"); }
  }

  return (
    <AppShell
      activeScreen={screen}
      error={error}
      onDismissError={() => setError(null)}
      onLogout={() => void handleLogout()}
      session={auth.session}
    >
      {screen === "today" && <TodayScreen onError={showError} timezone={auth.session.workspace.timezone} />}
      {screen === "transactions" && <TransactionsScreen initialAccountId={initialTransactionAccountId} onError={showError} openForm={openTransactionForm} />}
      {screen === "accounts" && (accountId ? <AccountDetailsScreen accountId={accountId} onError={showError} timezone={auth.session.workspace.timezone} /> : <AccountsScreen onError={showError} />)}
      {screen === "categories" && <CategoriesScreen onError={showError} />}
      {screen === "imports" && <ImportScreen onError={showError} />}
      {screen === "google" && <GoogleSheetsScreen onError={showError} />}
      {screen === "conflicts" && <SyncConflictsScreen onError={showError} />}
      {screen === "automations" && <AutomationsScreen onError={showError} />}
      {screen === "recurring" && <RecurringRulesScreen onError={showError} />}
      {screen === "telegram" && <TelegramScreen onError={showError} />}
      {screen === "month-close" && <MonthCloseScreen onError={showError} />}
      {screen === "budget" && <BudgetScreen onError={showError} preferredCurrency={auth.session.workspace.base_currency} timezone={auth.session.workspace.timezone} />}
      {screen === "reports" && <ReportsScreen onError={showError} timezone={auth.session.workspace.timezone} />}
      {screen === "payees" && <PayeesScreen onError={showError} role={auth.role} roleLoading={auth.roleLoading} />}
      {screen === "rules" && <ComingSoonScreen description="Правила категоризации — отдельный механизм и не заменяют recurring rules." requiresApi title="Правила" />}
      {screen === "goals" && <GoalsScreen onError={showError} preferredCurrency={auth.session.workspace.base_currency} />}
      {screen === "settings" && <ComingSoonScreen description="Настройки пространства будут перенесены после завершения интеграции рабочих финансовых экранов." title="Настройки" />}
    </AppShell>
  );
}
