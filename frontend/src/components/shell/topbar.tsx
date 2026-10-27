"use client";

import Link from "next/link";

import { ThemeToggle } from "./theme-toggle";
import { WorkspaceSwitcher } from "./workspace-switcher";

interface TopbarProps {
  onMenuOpen: () => void;
  onOpenCommandPalette: () => void;
  workspaceName: string;
}

export function Topbar({ onMenuOpen, onOpenCommandPalette, workspaceName }: TopbarProps) {
  return (
    <header className="shell-topbar">
      <button className="shell-icon-button shell-menu-button" type="button" onClick={onMenuOpen} aria-label="Открыть меню"><svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
      <WorkspaceSwitcher name={workspaceName} />
      <button className="shell-search" onClick={onOpenCommandPalette} type="button"><svg aria-hidden="true" fill="none" height="17" viewBox="0 0 24 24" width="17"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><span>Найти раздел или операцию…</span><kbd>Ctrl K</kbd></button>
      <div className="shell-topbar-actions"><span className="shell-period">Август 2026</span><ThemeToggle/><button className="shell-icon-button shell-notification" type="button" aria-label="Уведомления"><svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg><i /></button><Link className="primary-button shell-quick-add" href="/transactions?new=1"><span aria-hidden="true">＋</span> Операция</Link></div>
    </header>
  );
}
