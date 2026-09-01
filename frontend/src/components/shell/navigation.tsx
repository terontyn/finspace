import type { ReactNode } from "react";

export type AppScreen =
  | "today"
  | "transactions"
  | "accounts"
  | "budget"
  | "reports"
  | "categories"
  | "payees"
  | "rules"
  | "rules-review"
  | "goals"
  | "imports"
  | "google"
  | "conflicts"
  | "automations"
  | "recurring"
  | "telegram"
  | "month-close"
  | "settings";

/** Sub-screens keep their parent nav item highlighted. */
export function navigationScreen(screen: AppScreen): AppScreen {
  return screen === "rules-review" ? "rules" : screen;
}

/** Screens that own a navigation entry. Sub-screens like "rules-review" do not. */
export type NavScreen = Exclude<AppScreen, "rules-review">;

export interface NavigationItem {
  id: NavScreen;
  label: string;
  href: string;
  icon: ReactNode;
}

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18">
      {children}
    </svg>
  );
}

const icons: Record<NavScreen, ReactNode> = {
  today: <Icon><path d="M3 10.5 12 3l9 7.5M5 9.5V21h14V9.5M9 21v-7h6v7" /></Icon>,
  transactions: <Icon><path d="M7 4h10m-7-3L7 4l3 3m4 10h-4m7-3 3 3-3 3M7 17h10" /></Icon>,
  accounts: <Icon><rect height="15" rx="2" width="20" x="2" y="5"/><path d="M2 10h20M17 15h.01" /></Icon>,
  budget: <Icon><path d="M4 5h16v14H4zM4 10h16M10 10v9"/></Icon>,
  reports: <Icon><path d="M4 20V10m6 10V4m6 16v-7m4 7H2"/></Icon>,
  categories: <Icon><path d="M5 6h14M5 12h9M5 18h5"/><circle cx="3" cy="6" r=".5"/><circle cx="3" cy="12" r=".5"/><circle cx="3" cy="18" r=".5"/></Icon>,
  payees: <Icon><circle cx="9" cy="8" r="3"/><path d="M3 20c0-4 2-7 6-7s6 3 6 7m2-10h4m-2-2v4"/></Icon>,
  rules: <Icon><path d="M4 6h10m4 0h2M4 12h3m4 0h9M4 18h8m4 0h4"/><circle cx="16" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="14" cy="18" r="2"/></Icon>,
  goals: <Icon><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></Icon>,
  imports: <Icon><path d="M12 3v12m0 0 4-4m-4 4-4-4M4 18v3h16v-3" /></Icon>,
  google: <Icon><rect height="7" rx="1" width="7" x="3" y="3"/><rect height="7" rx="1" width="7" x="14" y="3"/><rect height="7" rx="1" width="7" x="3" y="14"/><rect height="7" rx="1" width="7" x="14" y="14"/></Icon>,
  conflicts: <Icon><path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 9v5m0 3h.01"/></Icon>,
  automations: <Icon><circle cx="12" cy="12" r="3"/><path d="M4.9 4.9a10 10 0 0 0 0 14.2m14.2 0a10 10 0 0 0 0-14.2"/></Icon>,
  recurring: <Icon><path d="M20 7V3l-2 2a9 9 0 0 0-15 6m1 6v4l2-2a9 9 0 0 0 15-6"/></Icon>,
  telegram: <Icon><path d="m21 3-8 18-4-7-6-3 18-8Z"/><path d="m9 14 5-5"/></Icon>,
  "month-close": <Icon><rect height="18" rx="2" width="18" x="3" y="4"/><path d="M8 2v4m8-4v4M3 9h18m-12 6 2 2 4-4"/></Icon>,
  settings: <Icon><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></Icon>,
};

export const navigationGroups: Array<{ label: string; items: NavigationItem[] }> = [
  {
    label: "Обзор",
    items: [
      { id: "today", label: "Сегодня", href: "/", icon: icons.today },
      { id: "transactions", label: "Операции", href: "/transactions", icon: icons.transactions },
      { id: "accounts", label: "Счета", href: "/accounts", icon: icons.accounts },
      { id: "reports", label: "Отчёты", href: "/reports", icon: icons.reports },
      { id: "categories", label: "Категории", href: "/categories", icon: icons.categories },
    ],
  },
  {
    label: "Планирование",
    items: [
      { id: "budget", label: "Бюджет", href: "/budget", icon: icons.budget },
      { id: "recurring", label: "Регулярные", href: "/recurring", icon: icons.recurring },
      { id: "goals", label: "Цели", href: "/goals", icon: icons.goals },
      { id: "payees", label: "Получатели", href: "/payees", icon: icons.payees },
      { id: "rules", label: "Правила", href: "/rules", icon: icons.rules },
      { id: "automations", label: "Автоматизации", href: "/automations", icon: icons.automations },
      { id: "month-close", label: "Закрытие месяца", href: "/month-close", icon: icons["month-close"] },
    ],
  },
  {
    label: "Интеграции",
    items: [
      { id: "imports", label: "Импорт", href: "/import", icon: icons.imports },
      { id: "google", label: "Google Sheets", href: "/integrations/google", icon: icons.google },
      { id: "telegram", label: "Telegram", href: "/integrations/telegram", icon: icons.telegram },
    ],
  },
  {
    label: "Система",
    items: [
      { id: "conflicts", label: "Конфликты", href: "/conflicts", icon: icons.conflicts },
      { id: "settings", label: "Настройки", href: "/settings", icon: icons.settings },
    ],
  },
];

export const navigationItems = navigationGroups.flatMap((group) => group.items);
