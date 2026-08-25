"use client";

import { useEffect, useRef, type ReactNode } from "react";

interface ActionDialogProps {
  children: ReactNode;
  description: string;
  eyebrow: string;
  onClose: () => void;
  title: string;
}

export function ActionDialog({ children, description, eyebrow, onClose, title }: ActionDialogProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => {
      dialogRef.current?.querySelector<HTMLElement>("input, textarea, button")?.focus();
    });
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled])",
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

  return <div className="action-dialog-backdrop" onMouseDown={onClose} role="presentation">
    <section aria-describedby="action-dialog-description" aria-labelledby="action-dialog-title" aria-modal="true" className="action-dialog" onMouseDown={(event) => event.stopPropagation()} ref={dialogRef} role="dialog">
      <header><div><span className="kicker">{eyebrow}</span><h2 id="action-dialog-title">{title}</h2><p id="action-dialog-description">{description}</p></div><button aria-label="Закрыть диалог" className="drawer-close" onClick={onClose} type="button">×</button></header>
      {children}
    </section>
  </div>;
}
