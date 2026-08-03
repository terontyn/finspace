"use client";

import { useCallback, useEffect, useState } from "react";

import { apiClient } from "@/lib/api-client";
import type { Category, CategoryTreeItem, CategoryType, Paged } from "@/types/finance";

interface CategoriesScreenProps {
  onError: (error: unknown) => void;
}

const categoryLabels: Record<CategoryType, string> = {
  income: "Доход",
  expense: "Расход",
  both: "Оба типа",
};

function CategoryBranch({
  item,
  onArchive,
}: {
  item: CategoryTreeItem;
  onArchive: (item: CategoryTreeItem) => void;
}) {
  return (
    <li>
      <div className="tree-item">
        <span className="category-swatch" style={{ background: item.color ?? "#80958b" }} />
        <div>
          <strong>{item.name}</strong>
          <span>{categoryLabels[item.category_type]}</span>
        </div>
        <button className="text-button text-button--danger" type="button" onClick={() => onArchive(item)}>
          В архив
        </button>
      </div>
      {item.children.length ? (
        <ul>
          {item.children.map((child) => (
            <CategoryBranch item={child} key={child.id} onArchive={onArchive} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function CategoriesScreen({ onError }: CategoriesScreenProps) {
  const [tree, setTree] = useState<CategoryTreeItem[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [name, setName] = useState("");
  const [categoryType, setCategoryType] = useState<CategoryType>("expense");
  const [parentId, setParentId] = useState("");
  const [color, setColor] = useState("#587f69");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const [treeResult, listResult] = await Promise.all([
        apiClient.get<CategoryTreeItem[]>("/api/v1/categories/tree"),
        apiClient.get<Paged<Category>>("/api/v1/categories?is_archived=false&limit=500"),
      ]);
      setTree(treeResult);
      setCategories(listResult.items);
    } catch (error) {
      onError(error);
    } finally {
      setIsLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setIsSaving(true);
    try {
      await apiClient.post<Category>("/api/v1/categories", {
        name,
        category_type: categoryType,
        parent_id: parentId || null,
        color,
      });
      setName("");
      setParentId("");
      await load();
    } catch (error) {
      onError(error);
    } finally {
      setIsSaving(false);
    }
  }

  async function archive(category: CategoryTreeItem) {
    try {
      await apiClient.delete<Category>(
        `/api/v1/categories/${category.id}?version=${category.version}`,
      );
      await load();
    } catch (error) {
      onError(error);
    }
  }

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Классификация</span>
          <h1>Категории</h1>
          <p>Иерархия доходов и расходов без циклических связей.</p>
        </div>
        <button className="secondary-button" type="button" onClick={() => void load()}>
          Обновить
        </button>
      </header>

      <div className="two-column">
        <div className="panel">
          <div className="panel-heading">
            <h2>Дерево категорий</h2>
            <span className="count-badge">{categories.length}</span>
          </div>
          {isLoading ? <div className="empty-state">Строим дерево…</div> : null}
          {!isLoading && !tree.length ? <div className="empty-state">Категорий пока нет.</div> : null}
          <ul className="category-tree">
            {tree.map((item) => (
              <CategoryBranch item={item} key={item.id} onArchive={(value) => void archive(value)} />
            ))}
          </ul>
        </div>

        <form className="form-panel" onSubmit={(event) => void create(event)}>
          <div className="panel-heading">
            <div>
              <span className="kicker">Новый объект</span>
              <h2>Добавить категорию</h2>
            </div>
          </div>
          <label>
            Название
            <input required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <label>
            Тип
            <select value={categoryType} onChange={(event) => setCategoryType(event.target.value as CategoryType)}>
              {Object.entries(categoryLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label>
            Родитель
            <select value={parentId} onChange={(event) => setParentId(event.target.value)}>
              <option value="">Корневая категория</option>
              {categories.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
            </select>
          </label>
          <label>
            Цвет
            <span className="color-field">
              <input type="color" value={color} onChange={(event) => setColor(event.target.value)} />
              <code>{color}</code>
            </span>
          </label>
          <button type="submit" disabled={isSaving}>{isSaving ? "Сохраняем…" : "Создать категорию"}</button>
        </form>
      </div>
    </section>
  );
}
