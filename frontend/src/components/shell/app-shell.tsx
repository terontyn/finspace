"use client";

import { useEffect, useState, type ReactNode } from "react";

import type { AuthSession } from "@/lib/api-client";

import { CommandPalette } from "./command-palette";
import { MobileNav } from "./mobile-nav";
import type { AppScreen } from "./navigation";
import { Sidebar } from "./sidebar";
import { Topbar } from "./topbar";

interface AppShellProps {
  activeScreen: AppScreen;
  children: ReactNode;
  error: string | null;
  onDismissError: () => void;
  onLogout: () => void;
  session: AuthSession;
}

export function AppShell({ activeScreen, children, error, onDismissError, onLogout, session }: AppShellProps) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setCommandOpen((value) => !value); }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, []);

  return <div className="app-shell"><Sidebar activeScreen={activeScreen} isOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} onLogout={onLogout} session={session}/><div className="shell-main"><Topbar onMenuOpen={() => setSidebarOpen(true)} onOpenCommandPalette={() => setCommandOpen(true)} workspaceName={session.workspace.name}/><main className="workspace">{error ? <div className="notice notice--error" role="alert"><span>{error}</span><button type="button" className="text-button" onClick={onDismissError}>Закрыть</button></div> : null}{children}</main></div><MobileNav activeScreen={activeScreen} onMore={() => setSidebarOpen(true)}/>{sidebarOpen ? <div className="shell-mobile-scrim" onClick={() => setSidebarOpen(false)} role="presentation"/> : null}<CommandPalette isOpen={commandOpen} onClose={() => setCommandOpen(false)}/></div>;
}
