import type { Category, CategoryType } from "@/types/finance";

export interface CategoryForm {
  categoryType: CategoryType;
  color: string;
  icon: string;
  name: string;
  parentId: string;
  sortOrder: string;
}

export function initialCategoryForm(parentId = ""): CategoryForm {
  return { categoryType: "expense", color: "#587f69", icon: "", name: "", parentId, sortOrder: "0" };
}

export function categoryFormFromRecord(category: Category): CategoryForm {
  return {
    categoryType: category.category_type,
    color: category.color ?? "#587f69",
    icon: category.icon ?? "",
    name: category.name,
    parentId: category.parent_id ?? "",
    sortOrder: String(category.sort_order),
  };
}

export function categoryPayload(form: CategoryForm) {
  return {
    category_type: form.categoryType,
    color: form.color || null,
    icon: form.icon || null,
    name: form.name,
    parent_id: form.parentId || null,
    sort_order: Number.parseInt(form.sortOrder, 10) || 0,
  };
}

export function categoryMutation(form: CategoryForm, editing: Category | null) {
  const payload = categoryPayload(form);
  return editing
    ? { method: "PATCH" as const, path: `/api/v1/categories/${editing.id}`, body: { ...payload, version: editing.version } }
    : { method: "POST" as const, path: "/api/v1/categories", body: payload };
}

export function categoryArchiveMutation(category: Category, isArchived: boolean) {
  return { path: `/api/v1/categories/${category.id}`, body: { version: category.version, is_archived: isArchived } };
}

export function categoryDescendantIds(categories: Category[], categoryId: string): Set<string> {
  const result = new Set<string>();
  const queue = [categoryId];
  while (queue.length) {
    const parentId = queue.shift();
    for (const category of categories) {
      if (category.parent_id === parentId && !result.has(category.id)) {
        result.add(category.id);
        queue.push(category.id);
      }
    }
  }
  return result;
}
