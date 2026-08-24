"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import { navigationItems } from "./navigation";

interface CommandPaletteProps {
  isOpen: boolean;
  onClose: () => void;
}

export function CommandPalette({ isOpen, onClose }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const matches = useMemo(() => navigationItems.filter((item) => item.label.toLocaleLowerCase("ru").includes(query.toLocaleLowerCase("ru"))), [query]);

  useEffect(() => {
    if (!isOpen) return;
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    const handleKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", handleKey);
    return () => { window.cancelAnimationFrame(frame); window.removeEventListener("keydown", handleKey); };
  }, [isOpen, onClose]);

  if (!isOpen) return null;
  return <div className="command-palette-backdrop" onMouseDown={onClose} role="presentation"><section aria-label="Командная палитра" aria-modal="true" className="command-palette" onMouseDown={(event) => event.stopPropagation()} role="dialog"><div className="command-palette-input"><svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input ref={inputRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Перейти к разделу…"/><kbd>Esc</kbd></div><span className="command-palette-label">Быстрый переход</span><div className="command-palette-results">{matches.map((item) => <Link href={item.href} key={item.id} onClick={onClose}><span className="nav-icon">{item.icon}</span><strong>{item.label}</strong><small>Открыть</small><kbd>↵</kbd></Link>)}{!matches.length ? <p>Ничего не найдено.</p> : null}</div></section></div>;
}
