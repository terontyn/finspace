"use client";

import type { AuthSession } from "@/lib/api-client";
import Link from "next/link";

import { navigationGroups, type AppScreen } from "./navigation";

interface SidebarProps {
  activeScreen: AppScreen;
  isOpen: boolean;
  onClose: () => void;
  onLogout: () => void;
  session: AuthSession;
}

export function Sidebar({ activeScreen, isOpen, onClose, onLogout, session }: SidebarProps) {
  const initials = session.user.display_name.split(" ").map((word) => word[0]).join("").slice(0, 2).toUpperCase();

  return (
    <aside className={isOpen ? "sidebar sidebar--open" : "sidebar"}>
      <div className="brand">
        <div className="brand-logo">Ф</div>
        <div className="brand-text"><strong className="brand-name">Финпространство</strong><span className="brand-workspace">личные финансы</span></div>
        <button className="sidebar-close" type="button" onClick={onClose} aria-label="Закрыть меню">×</button>
      </div>
      <nav aria-label="Основная навигация" className="shell-navigation">
        {navigationGroups.map((group) => (
          <div className="shell-navigation-group" key={group.label}>
            <span className="shell-navigation-label">{group.label}</span>
            {group.items.map((item) => (
              <Link className={activeScreen === item.id ? "nav-item nav-item--active" : "nav-item"} href={item.href} key={item.id} onClick={onClose}>
                <span className="nav-icon">{item.icon}</span><span>{item.label}</span>
              </Link>
            ))}
          </div>
        ))}
      </nav>
      <div className="sidebar-session"><div className="sidebar-user"><div className="sidebar-avatar" title={session.user.display_name}>{initials}</div><div className="sidebar-user-info"><span className="sidebar-user-name">{session.user.display_name}</span><span className="sidebar-user-email">{session.user.email}</span></div><button className="sidebar-logout" type="button" onClick={onLogout} title="Выйти из аккаунта" aria-label="Выйти из аккаунта"><svg aria-hidden="true" fill="none" height="17" viewBox="0 0 24 24" width="17"><path d="M10 4H5v16h5m5-4 4-4-4-4m4 4H9"/></svg></button></div></div>
    </aside>
  );
}
