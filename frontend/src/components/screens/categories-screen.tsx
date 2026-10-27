"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { EntityDrawer } from "@/components/ui/entity-drawer";
import { apiClient } from "@/lib/api-client";
import type { Category, CategoryTreeItem, CategoryType, Paged } from "@/types/finance";

import { categoryArchiveMutation, categoryDescendantIds, categoryFormFromRecord, categoryMutation, initialCategoryForm, type CategoryForm } from "./category-form";

interface CategoriesScreenProps { onError: (error: unknown) => void; }

const categoryLabels: Record<CategoryType, string> = { both: "Доходы и расходы", expense: "Расходы", income: "Доходы" };
const categoryTypes: CategoryType[] = ["expense", "income", "both"];

function CategoryBranch({ item, onArchive, onCreateChild, onEdit }: {
  item: CategoryTreeItem;
  onArchive: (category: Category) => void;
  onCreateChild: (category: Category) => void;
  onEdit: (category: Category) => void;
}) {
  return <li className="category-node">
    <div className="category-node-row">
      <span className="category-node-symbol" style={{ "--category-color": item.color ?? "#80958b" } as React.CSSProperties}>{item.icon || item.name.slice(0, 1).toUpperCase()}</span>
      <div><strong>{item.name}</strong><span>{categoryLabels[item.category_type]} · порядок {item.sort_order}</span></div>
      <span className="category-version">v{item.version}</span>
      <div className="category-node-actions"><button className="text-button" onClick={() => onCreateChild(item)} type="button">＋ Вложенная</button><button className="text-button" onClick={() => onEdit(item)} type="button">Изменить</button><button className="text-button text-button--danger" onClick={() => onArchive(item)} type="button">В архив</button></div>
    </div>
    {item.children.length ? <ul>{item.children.map((child) => <CategoryBranch item={child} key={child.id} onArchive={onArchive} onCreateChild={onCreateChild} onEdit={onEdit}/>)}</ul> : null}
  </li>;
}

function CategoryDrawer({ categories, editing, form, isSaving, onChange, onClose, onSave }: {
  categories: Category[];
  editing: Category | null;
  form: CategoryForm;
  isSaving: boolean;
  onChange: (form: CategoryForm) => void;
  onClose: () => void;
  onSave: (event: React.FormEvent) => void;
}) {
  const excludedParents = useMemo(() => editing ? categoryDescendantIds(categories, editing.id) : new Set<string>(), [categories, editing]);
  const parentOptions = categories.filter((category) => category.id !== editing?.id && !excludedParents.has(category.id));
  const pickerColor = /^#[0-9a-f]{6}$/i.test(form.color) ? form.color : "#587f69";
  return <EntityDrawer ariaLabel={editing ? "Редактирование категории" : "Новая категория"} eyebrow={editing ? "Редактирование" : "Новый объект"} onClose={onClose} subtitle={editing ? `Версия ${editing.version}` : "Иерархия проверяется backend"} title={editing ? editing.name : "Добавить категорию"}>
    <form className="entity-form" onSubmit={onSave}>
      <label><span>Название</span><input maxLength={200} onChange={(event) => onChange({ ...form, name: event.target.value })} required value={form.name}/></label>
      <div className="entity-form-grid"><label><span>Тип</span><select onChange={(event) => onChange({ ...form, categoryType: event.target.value as CategoryType })} value={form.categoryType}>{Object.entries(categoryLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label><span>Порядок</span><input inputMode="numeric" onChange={(event) => onChange({ ...form, sortOrder: event.target.value })} pattern="-?\d+" required value={form.sortOrder}/></label></div>
      <label><span>Родительская категория</span><select onChange={(event) => onChange({ ...form, parentId: event.target.value })} value={form.parentId}><option value="">Корневая категория</option>{parentOptions.map((category) => <option key={category.id} value={category.id}>{category.name} · {categoryLabels[category.category_type]}</option>)}</select></label>
      <div className="entity-form-grid"><label><span>Цвет</span><div className="category-color-field"><input aria-label="Выбрать цвет" onChange={(event) => onChange({ ...form, color: event.target.value })} type="color" value={pickerColor}/><input maxLength={20} onChange={(event) => onChange({ ...form, color: event.target.value })} value={form.color}/></div></label><label><span>Иконка</span><input maxLength={50} onChange={(event) => onChange({ ...form, icon: event.target.value })} placeholder="Символ или короткое имя" value={form.icon}/></label></div>
      <p className="entity-form-note">Порядок сохраняется в поле <code>sort_order</code>. Drag-and-drop не используется, потому что API не предоставляет атомарную пакетную перестановку.</p>
      <footer><button className="secondary-button" onClick={onClose} type="button">Отмена</button><button className="primary-button" disabled={isSaving} type="submit">{isSaving ? "Сохраняем…" : editing ? "Сохранить изменения" : "Создать категорию"}</button></footer>
    </form>
  </EntityDrawer>;
}

export function CategoriesScreen({ onError }: CategoriesScreenProps) {
  const [tree, setTree] = useState<CategoryTreeItem[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [archived, setArchivedCategories] = useState<Category[]>([]);
  const [editing, setEditing] = useState<Category | null>(null);
  const [form, setForm] = useState<CategoryForm>(() => initialCategoryForm());
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  const counts = useMemo(() => Object.fromEntries(categoryTypes.map((type) => [type, categories.filter((category) => category.category_type === type).length])) as Record<CategoryType, number>, [categories]);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const [treeResult, activeResult, archivedResult] = await Promise.all([
        apiClient.get<CategoryTreeItem[]>("/api/v1/categories/tree?is_archived=false"),
        apiClient.get<Paged<Category>>("/api/v1/categories?is_archived=false&limit=500"),
        apiClient.get<Paged<Category>>("/api/v1/categories?is_archived=true&limit=500"),
      ]);
      setTree(treeResult); setCategories(activeResult.items); setArchivedCategories(archivedResult.items);
    } catch (error) { onError(error); } finally { setIsLoading(false); }
  }, [onError]);

  useEffect(() => { queueMicrotask(() => void load()); }, [load]);

  function openCreate(parentId = "") { setEditing(null); setForm(initialCategoryForm(parentId)); setDrawerOpen(true); }
  function startEdit(category: Category) { setEditing(category); setForm(categoryFormFromRecord(category)); setDrawerOpen(true); }
  function closeDrawer() { setDrawerOpen(false); setEditing(null); setForm(initialCategoryForm()); }

  async function save(event: React.FormEvent) {
    event.preventDefault(); setIsSaving(true);
    const mutation = categoryMutation(form, editing);
    try {
      if (mutation.method === "PATCH") await apiClient.patch<Category>(mutation.path, mutation.body);
      else await apiClient.post<Category>(mutation.path, mutation.body);
      closeDrawer(); await load();
    } catch (error) { onError(error); } finally { setIsSaving(false); }
  }

  async function changeArchivedState(category: Category, isArchived: boolean) {
    const mutation = categoryArchiveMutation(category, isArchived);
    try { await apiClient.patch<Category>(mutation.path, mutation.body); await load(); } catch (error) { onError(error); }
  }

  return <section>
    <header className="screen-header"><div><span className="kicker">Классификация</span><h1>Категории</h1><p>Иерархия доходов и расходов с версиями, архивом и безопасной проверкой циклов.</p></div><div className="screen-header-actions"><button className="secondary-button" disabled={isLoading} onClick={() => void load()} type="button">Обновить</button><button className="primary-button" onClick={() => openCreate()} type="button">＋ Категория</button></div></header>

    <section aria-label="Сводка по категориям" className="category-summary-grid">
      {categoryTypes.map((type) => <article key={type}><span>{categoryLabels[type]}</span><strong>{counts[type]}</strong><small>активных категорий</small></article>)}
      <article><span>Архив</span><strong>{archived.length}</strong><small>можно восстановить</small></article>
    </section>

    {isLoading ? <div className="category-tree-skeleton">{Array.from({ length: 4 }, (_, index) => <i key={index}/>)}</div> : <div className="category-groups">{categoryTypes.map((type) => {
      const roots = tree.filter((item) => item.category_type === type);
      return <section className="panel category-group" key={type}><div className="panel-heading"><div><span className="kicker">Тип</span><h2>{categoryLabels[type]}</h2></div><span className="count-badge">{counts[type]}</span></div>{roots.length ? <ul className="production-category-tree">{roots.map((item) => <CategoryBranch item={item} key={item.id} onArchive={(value) => void changeArchivedState(value, true)} onCreateChild={(value) => openCreate(value.id)} onEdit={startEdit}/>)}</ul> : <div className="empty-state"><strong>Корневых категорий нет</strong><span>Создайте категорию этого типа или измените существующую.</span></div>}</section>;
    })}</div>}

    <section className="panel category-archive-section"><div className="panel-heading"><div><span className="kicker">Не используются</span><h2>Архив категорий</h2></div><span className="count-badge">{archived.length}</span></div>{archived.length ? <div className="category-archive-list">{archived.map((category) => <article key={category.id}><span className="category-node-symbol" style={{ "--category-color": category.color ?? "#80958b" } as React.CSSProperties}>{category.icon || category.name.slice(0, 1).toUpperCase()}</span><div><strong>{category.name}</strong><span>{categoryLabels[category.category_type]} · порядок {category.sort_order} · v{category.version}</span></div><button className="text-button" onClick={() => void changeArchivedState(category, false)} type="button">Восстановить</button></article>)}</div> : <div className="empty-state"><strong>Архив пуст</strong><span>Архивированные категории появятся здесь.</span></div>}</section>

    {drawerOpen ? <CategoryDrawer categories={categories} editing={editing} form={form} isSaving={isSaving} onChange={setForm} onClose={closeDrawer} onSave={(event) => void save(event)}/> : null}
  </section>;
}
