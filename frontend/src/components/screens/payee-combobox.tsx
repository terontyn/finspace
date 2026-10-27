"use client";

import { useEffect, useId, useRef, useState } from "react";

import { apiClient } from "@/lib/api-client";
import type { EntityRef, Paged, Payee } from "@/types/finance";

interface PayeeComboboxProps {
  initialSelection: EntityRef | null;
  onChange: (payeeId: string) => void;
  onError: (error: unknown) => void;
  value: string;
}

export function PayeeCombobox({ initialSelection, onChange, onError, value }: PayeeComboboxProps) {
  const listboxId = useId();
  const blurTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestId = useRef(0);
  const [query, setQuery] = useState(initialSelection?.name ?? "");
  const [selected, setSelected] = useState<EntityRef | null>(initialSelection);
  const [items, setItems] = useState<Payee[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  useEffect(() => {
    if (!open) return;
    const currentRequest = ++requestId.current;
    const params = new URLSearchParams({ limit: "20", offset: "0" });
    const normalized = query.trim();
    if (normalized && normalized !== selected?.name) params.set("search", normalized);
    queueMicrotask(() => {
      if (requestId.current === currentRequest) setLoading(true);
    });
    void apiClient.get<Paged<Payee>>(`/api/v1/payees?${params.toString()}`).then((result) => {
      if (requestId.current !== currentRequest) return;
      setItems(result.items.filter((payee) => !payee.deleted_at));
      setActiveIndex(0);
    }).catch((error: unknown) => {
      if (requestId.current === currentRequest) onError(error);
    }).finally(() => {
      if (requestId.current === currentRequest) setLoading(false);
    });
  }, [onError, open, query, selected?.name]);

  useEffect(() => () => {
    if (blurTimer.current) clearTimeout(blurTimer.current);
  }, []);

  function choose(payee: Payee) {
    const next = { id: payee.id, name: payee.name };
    setSelected(next);
    setQuery(payee.name);
    setOpen(false);
    onChange(payee.id);
  }

  function clearSelection() {
    setSelected(null);
    setQuery("");
    setOpen(false);
    onChange("");
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape" && open) {
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      setQuery(selected?.name ?? "");
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((current) => Math.min(items.length - 1, current + 1));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => Math.max(0, current - 1));
      return;
    }
    if (event.key === "Enter" && open && items[activeIndex]) {
      event.preventDefault();
      choose(items[activeIndex]);
    }
  }

  return <div className="payee-combobox">
    <label htmlFor={`${listboxId}-input`}><span>Получатель</span></label>
    <div className="payee-combobox-input">
      <input
        aria-activedescendant={open && items[activeIndex] ? `${listboxId}-${items[activeIndex].id}` : undefined}
        aria-autocomplete="list"
        aria-controls={listboxId}
        aria-expanded={open}
        autoComplete="off"
        id={`${listboxId}-input`}
        onBlur={() => {
          blurTimer.current = setTimeout(() => {
            setOpen(false);
            setQuery(selected?.name ?? "");
          }, 100);
        }}
        onChange={(event) => { setQuery(event.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onKeyDown={handleKeyDown}
        placeholder="Найти по имени или алиасу"
        role="combobox"
        value={query}
      />
      {value ? <button aria-label="Очистить получателя" className="payee-combobox-clear" onMouseDown={(event) => event.preventDefault()} onClick={clearSelection} type="button">×</button> : null}
    </div>
    {value && selected ? <small className="payee-combobox-selection">Назначен: {selected.name}</small> : <small className="payee-combobox-selection">Без получателя — выберите запись явно</small>}
    {open ? <div className="payee-combobox-menu" id={listboxId} role="listbox">
      <button aria-selected={!value} className={!value ? "is-selected" : ""} onMouseDown={(event) => event.preventDefault()} onClick={clearSelection} role="option" type="button">Без получателя</button>
      {loading ? <span className="payee-combobox-state">Ищем получателей…</span> : items.length ? items.map((payee, index) => <button aria-selected={value === payee.id} className={index === activeIndex ? "is-active" : ""} id={`${listboxId}-${payee.id}`} key={payee.id} onMouseDown={(event) => event.preventDefault()} onClick={() => choose(payee)} role="option" type="button"><strong>{payee.name}</strong><small>{payee.aliases.filter((alias) => !alias.is_primary && !alias.deleted_at).map((alias) => alias.alias).join(" · ") || "Без вторичных алиасов"}</small></button>) : <span className="payee-combobox-state">Активные получатели не найдены</span>}
    </div> : null}
  </div>;
}
