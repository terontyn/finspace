"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { EntityDrawer } from "@/components/ui/entity-drawer";
import { apiClient, type WorkspaceRole } from "@/lib/api-client";
import type { Paged, Payee, PayeeAlias } from "@/types/finance";

import {
  initialPayeeForm,
  payeeAliasArchiveMutation,
  payeeAliasCreateMutation,
  payeeAliasRestoreMutation,
  payeeArchiveMutation,
  payeeFormFromRecord,
  payeeMutation,
  payeeRestoreMutation,
  type PayeeForm,
} from "./payee-form";

interface PayeesScreenProps {
  onError: (error: unknown) => void;
  role: WorkspaceRole | null;
  roleLoading: boolean;
}

const limit = 12;

function PayeeAliases({ payee }: { payee: Payee }) {
  const aliases = [...payee.aliases].sort((left, right) => Number(right.is_primary) - Number(left.is_primary));
  return <div className="payee-alias-chips" aria-label={`Алиасы ${payee.name}`}>
    {aliases.map((alias) => <span className={alias.deleted_at ? "payee-alias-chip is-archived" : alias.is_primary ? "payee-alias-chip is-primary" : "payee-alias-chip"} key={alias.id}>
      {alias.alias}<small>{alias.is_primary ? "основной" : alias.deleted_at ? "в архиве" : "алиас"}</small>
    </span>)}
  </div>;
}

function AliasManager({ aliasValue, isSaving, onAliasValueChange, onArchive, onCreate, onRestore, payee }: {
  aliasValue: string;
  isSaving: boolean;
  onAliasValueChange: (value: string) => void;
  onArchive: (alias: PayeeAlias) => void;
  onCreate: (event: React.FormEvent) => void;
  onRestore: (alias: PayeeAlias) => void;
  payee: Payee;
}) {
  return <section className="payee-alias-manager">
    <div><span className="kicker">Имена из источников</span><h3>Алиасы</h3><p>Алиасы не связывают операции автоматически.</p></div>
    <form className="payee-alias-create" onSubmit={onCreate}>
      <label><span>Новый вторичный алиас</span><input maxLength={300} onChange={(event) => onAliasValueChange(event.target.value)} placeholder="Например, название из выписки" required value={aliasValue}/></label>
      <button className="secondary-button" disabled={isSaving || !aliasValue.trim()} type="submit">Добавить</button>
    </form>
    <div className="payee-alias-list">
      {[...payee.aliases].sort((left, right) => Number(right.is_primary) - Number(left.is_primary)).map((alias) => <article className={alias.deleted_at ? "is-archived" : ""} key={alias.id}>
        <div><strong>{alias.alias}</strong><span>{alias.is_primary ? "Основной алиас — совпадает с именем получателя" : alias.deleted_at ? "Архивирован" : "Вторичный алиас"}</span></div>
        {!alias.is_primary ? alias.deleted_at
          ? <button className="text-button" disabled={isSaving} onClick={() => onRestore(alias)} type="button">Восстановить</button>
          : <button className="text-button text-button--danger" disabled={isSaving} onClick={() => onArchive(alias)} type="button">В архив</button>
          : <span className="status-chip">Основной</span>}
      </article>)}
    </div>
  </section>;
}

function PayeeDrawer({ aliasValue, editing, form, isSaving, onAliasArchive, onAliasCreate, onAliasRestore, onAliasValueChange, onChange, onClose, onSave }: {
  aliasValue: string;
  editing: Payee | null;
  form: PayeeForm;
  isSaving: boolean;
  onAliasArchive: (alias: PayeeAlias) => void;
  onAliasCreate: (event: React.FormEvent) => void;
  onAliasRestore: (alias: PayeeAlias) => void;
  onAliasValueChange: (value: string) => void;
  onChange: (value: PayeeForm) => void;
  onClose: () => void;
  onSave: (event: React.FormEvent) => void;
}) {
  return <EntityDrawer ariaLabel={editing ? "Редактирование получателя" : "Новый получатель"} eyebrow={editing ? "Каноническая запись" : "Новый объект"} onClose={onClose} subtitle={editing ? `Версия ${editing.version}` : "Основной алиас создаст backend"} title={editing?.name ?? "Добавить получателя"}>
    <form className="entity-form" onSubmit={onSave}>
      <label><span>Название</span><input autoComplete="off" maxLength={300} onChange={(event) => onChange({ ...form, name: event.target.value })} required value={form.name}/></label>
      <label><span>Заметки</span><textarea onChange={(event) => onChange({ ...form, notes: event.target.value })} placeholder="Необязательно" rows={4} value={form.notes}/></label>
      <p className="entity-form-note"><code>counterparty</code> остаётся независимым исходным текстом. Изменение получателя или алиаса не меняет операции.</p>
      <footer><button className="secondary-button" onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={isSaving || !form.name.trim()} type="submit">{isSaving ? "Сохраняем…" : editing ? "Сохранить" : "Создать"}</button></footer>
    </form>
    {editing ? <AliasManager aliasValue={aliasValue} isSaving={isSaving} onAliasValueChange={onAliasValueChange} onArchive={onAliasArchive} onCreate={onAliasCreate} onRestore={onAliasRestore} payee={editing}/> : null}
  </EntityDrawer>;
}

export function PayeesScreen({ onError, role, roleLoading }: PayeesScreenProps) {
  const [items, setItems] = useState<Payee[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<Payee | null>(null);
  const [form, setForm] = useState<PayeeForm>(() => initialPayeeForm());
  const [aliasValue, setAliasValue] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const canWrite = role === "owner" || role === "editor";

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (appliedSearch) params.set("search", appliedSearch);
    if (includeDeleted) params.set("include_deleted", "true");
    return params.toString();
  }, [appliedSearch, includeDeleted, offset]);

  const load = useCallback(async () => {
    let redirectedToValidPage = false;
    setIsLoading(true);
    setLoadFailed(false);
    try {
      const result = await apiClient.get<Paged<Payee>>(`/api/v1/payees?${query}`);
      const lastOffset = result.page.total > 0 ? Math.floor((result.page.total - 1) / limit) * limit : 0;
      if (result.page.offset > lastOffset) {
        redirectedToValidPage = true;
        setOffset(lastOffset);
        return;
      }
      setItems(result.items);
      setTotal(result.page.total);
    } catch (error) {
      setLoadFailed(true);
      onError(error);
    } finally {
      if (!redirectedToValidPage) setIsLoading(false);
    }
  }, [onError, query]);

  useEffect(() => { queueMicrotask(() => void load()); }, [load]);

  function openCreate() {
    setEditing(null);
    setForm(initialPayeeForm());
    setAliasValue("");
    setDrawerOpen(true);
  }

  function startEdit(payee: Payee) {
    setEditing(payee);
    setForm(payeeFormFromRecord(payee));
    setAliasValue("");
    setDrawerOpen(true);
  }

  function closeDrawer() {
    setDrawerOpen(false);
    setEditing(null);
    setForm(initialPayeeForm());
    setAliasValue("");
  }

  function replacePayee(payee: Payee) {
    setItems((current) => current.map((item) => item.id === payee.id ? payee : item));
    setEditing((current) => current?.id === payee.id ? payee : current);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!canWrite) return;
    setIsSaving(true);
    const mutation = payeeMutation(form, editing);
    try {
      if (mutation.method === "PATCH") await apiClient.patch<Payee>(mutation.path, mutation.body);
      else await apiClient.post<Payee>(mutation.path, mutation.body);
      closeDrawer();
      await load();
    } catch (error) { onError(error); } finally { setIsSaving(false); }
  }

  async function archive(payee: Payee) {
    if (!canWrite) return;
    const mutation = payeeArchiveMutation(payee);
    try { await apiClient.delete<Payee>(mutation.path); await load(); } catch (error) { onError(error); }
  }

  async function restore(payee: Payee) {
    if (!canWrite) return;
    const mutation = payeeRestoreMutation(payee);
    try { await apiClient.post<Payee>(mutation.path, mutation.body); await load(); } catch (error) { onError(error); }
  }

  async function createAlias(event: React.FormEvent) {
    event.preventDefault();
    if (!canWrite || !editing || !aliasValue.trim()) return;
    setIsSaving(true);
    const mutation = payeeAliasCreateMutation(editing, aliasValue);
    try {
      const updated = await apiClient.post<Payee>(mutation.path, mutation.body);
      replacePayee(updated);
      setAliasValue("");
    } catch (error) { onError(error); } finally { setIsSaving(false); }
  }

  async function archiveAlias(alias: PayeeAlias) {
    if (!canWrite || !editing) return;
    setIsSaving(true);
    const mutation = payeeAliasArchiveMutation(editing, alias);
    try { replacePayee(await apiClient.delete<Payee>(mutation.path)); } catch (error) { onError(error); } finally { setIsSaving(false); }
  }

  async function restoreAlias(alias: PayeeAlias) {
    if (!canWrite || !editing) return;
    setIsSaving(true);
    const mutation = payeeAliasRestoreMutation(editing, alias);
    try { replacePayee(await apiClient.post<Payee>(mutation.path, mutation.body)); } catch (error) { onError(error); } finally { setIsSaving(false); }
  }

  const emptyTitle = appliedSearch ? "Получатели не найдены" : includeDeleted ? "Получателей нет" : "Получателей пока нет";
  const emptyText = appliedSearch ? "Измените поисковый запрос или сбросьте фильтры." : "Создайте первую каноническую запись получателя.";

  return <section className="payees-screen">
    <header className="screen-header"><div><span className="kicker">Канонические контрагенты</span><h1>Получатели</h1><p>Явные назначения для операций; исходный counterparty и алиасы остаются независимыми.</p></div><div className="screen-header-actions"><button className="secondary-button" disabled={isLoading} onClick={() => void load()} type="button">Обновить</button>{canWrite ? <button className="primary-button" onClick={openCreate} type="button">＋ Получатель</button> : null}</div></header>

    {!roleLoading && role === "viewer" ? <div className="notice notice--info" role="status"><span>Режим просмотра: создание, изменение, архив и управление алиасами доступны редактору или владельцу.</span></div> : null}
    {!roleLoading && role === null ? <div className="notice notice--warning" role="status"><span>Не удалось подтвердить права записи. Управление получателями временно отключено.</span></div> : null}

    <section className="panel payee-directory">
      <form className="payee-filters" onSubmit={(event) => { event.preventDefault(); setOffset(0); setAppliedSearch(search.trim()); }}>
        <label className="payee-search"><span>Поиск</span><input aria-label="Поиск получателей" maxLength={300} onChange={(event) => setSearch(event.target.value)} placeholder="Имя или алиас" value={search}/></label>
        <button className="secondary-button" type="submit">Найти</button>
        <label className="payee-archive-filter"><input checked={includeDeleted} onChange={(event) => { setOffset(0); setIncludeDeleted(event.target.checked); }} type="checkbox"/><span>Показывать архив</span></label>
        {(appliedSearch || includeDeleted) ? <button className="text-button" onClick={() => { setSearch(""); setAppliedSearch(""); setIncludeDeleted(false); setOffset(0); }} type="button">Сбросить</button> : null}
      </form>
      <div className="payee-result-meta"><span>Найдено: <strong>{total}</strong></span>{roleLoading ? <span>Проверяем права…</span> : <span>{canWrite ? "Редактирование доступно" : "Только просмотр"}</span>}</div>

      {isLoading ? <div aria-label="Загружаем получателей" className="payee-list-skeleton">{Array.from({ length: 4 }, (_, index) => <i key={index}/>)}</div> : loadFailed ? <div className="empty-state payee-load-error"><strong>Не удалось загрузить получателей</strong><span>Повторите запрос. Подробности показаны в общем уведомлении.</span><button className="secondary-button" onClick={() => void load()} type="button">Повторить</button></div> : items.length ? <div className="payee-list">{items.map((payee) => <article className={payee.deleted_at ? "payee-row is-archived" : "payee-row"} data-payee-id={payee.id} key={payee.id}>
        <span className="payee-symbol">{payee.name.slice(0, 1).toUpperCase()}</span>
        <div className="payee-primary"><div><strong>{payee.name}</strong>{payee.deleted_at ? <span className="status-chip status-chip--cancelled">В архиве</span> : <span className="status-chip status-chip--confirmed">Активен</span>}</div><p>{payee.notes ?? "Без заметок"}</p><PayeeAliases payee={payee}/></div>
        <div className="payee-version"><span>Версия</span><strong>v{payee.version}</strong></div>
        {canWrite ? <div className="payee-actions">{payee.deleted_at ? <button className="text-button" onClick={() => void restore(payee)} type="button">Восстановить</button> : <><button className="text-button" onClick={() => startEdit(payee)} type="button">Изменить</button><button className="text-button text-button--danger" onClick={() => void archive(payee)} type="button">В архив</button></>}</div> : null}
      </article>)}</div> : <div className="empty-state payee-empty-state"><span className="empty-state-icon">◎</span><strong>{emptyTitle}</strong><span>{emptyText}</span>{canWrite && !appliedSearch ? <button className="primary-button" onClick={openCreate} type="button">Создать получателя</button> : null}</div>}

      <div className="pagination"><span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} из ${total}` : "0 получателей"}</span><div><button className="secondary-button" disabled={offset === 0 || isLoading} onClick={() => setOffset(Math.max(0, offset - limit))} type="button">Назад</button><button className="secondary-button" disabled={offset + limit >= total || isLoading} onClick={() => setOffset(offset + limit)} type="button">Дальше</button></div></div>
    </section>

    {drawerOpen && canWrite ? <PayeeDrawer aliasValue={aliasValue} editing={editing} form={form} isSaving={isSaving} onAliasArchive={(alias) => void archiveAlias(alias)} onAliasCreate={(event) => void createAlias(event)} onAliasRestore={(alias) => void restoreAlias(alias)} onAliasValueChange={setAliasValue} onChange={setForm} onClose={closeDrawer} onSave={(event) => void save(event)}/> : null}
  </section>;
}
