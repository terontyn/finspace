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
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previous = typeof document !== "undefined" && typeof HTMLElement !== "undefined" && document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const frame = window.requestAnimationFrame(() => {
      const contentControl = drawerRef.current?.querySelector<HTMLElement>(
        'input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled])',
      );
      const fallbackControl = drawerRef.current?.querySelector<HTMLElement>("button:not([disabled])");
      (contentControl ?? fallbackControl)?.focus();
    });
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
      if (event.key !== "Tab" || !drawerRef.current) return;
      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
        ),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", handleKey);
      previous?.focus();
    };
  }, []);

  return <div className="entity-drawer-backdrop" onMouseDown={onClose} role="presentation">
    <section aria-label={ariaLabel} aria-modal="true" className="entity-drawer" onMouseDown={(event) => event.stopPropagation()} ref={drawerRef} role="dialog">
      <header><div><span className="kicker">{eyebrow}</span><h2>{title}</h2>{subtitle ? <small>{subtitle}</small> : null}</div><button aria-label="Закрыть" className="drawer-close" onClick={onClose} type="button">×</button></header>
      {children}
    </section>
  </div>;
}
