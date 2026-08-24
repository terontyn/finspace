"use client";

import { useEffect, useRef, type ReactNode } from "react";

interface EntityDrawerProps {
  ariaLabel: string;
  children: ReactNode;
  eyebrow: string;
  onClose: () => void;
  subtitle?: string;
  title: string;
}

export function EntityDrawer({ ariaLabel, children, eyebrow, onClose, subtitle, title }: EntityDrawerProps) {
  const drawerRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => drawerRef.current?.querySelector<HTMLElement>("input, select, button")?.focus());
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab" || !drawerRef.current) return;
      const focusable = Array.from(drawerRef.current.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])'));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", handleKey);
    return () => { window.cancelAnimationFrame(frame); window.removeEventListener("keydown", handleKey); };
  }, [onClose]);

  return <div className="entity-drawer-backdrop" onMouseDown={onClose} role="presentation">
    <section aria-label={ariaLabel} aria-modal="true" className="entity-drawer" onMouseDown={(event) => event.stopPropagation()} ref={drawerRef} role="dialog">
      <header><div><span className="kicker">{eyebrow}</span><h2>{title}</h2>{subtitle ? <small>{subtitle}</small> : null}</div><button aria-label="Закрыть" className="drawer-close" onClick={onClose} type="button">×</button></header>
      {children}
    </section>
  </div>;
}
