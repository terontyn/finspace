import assert from "node:assert/strict";
import test from "node:test";

import type { Category } from "@/types/finance";

import { categoryArchiveMutation, categoryDescendantIds, categoryMutation, initialCategoryForm } from "./category-form";

function category(id: string, parentId: string | null, version = 1): Category {
  return { id, parent_id: parentId, name: id, category_type: "expense", color: null, icon: null, sort_order: 0, is_archived: false, version, created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z" };
}

test("category create and edit preserve hierarchy, order and optimistic version", () => {
  const form = { ...initialCategoryForm("parent"), icon: "cart", name: "Продукты", sortOrder: "30" };
  const create = categoryMutation(form, null);
  assert.equal(create.method, "POST");
  assert.deepEqual(create.body, { category_type: "expense", color: "#587f69", icon: "cart", name: "Продукты", parent_id: "parent", sort_order: 30 });

  const edit = categoryMutation(form, category("child", "parent", 8));
  assert.equal(edit.method, "PATCH");
  assert.equal(edit.body.version, 8);
});

test("category lifecycle and parent choices protect the hierarchy", () => {
  const root = category("root", null, 3);
  assert.deepEqual(categoryArchiveMutation(root, true).body, { version: 3, is_archived: true });
  assert.deepEqual(categoryArchiveMutation(root, false).body, { version: 3, is_archived: false });
  assert.deepEqual(categoryDescendantIds([root, category("child", "root"), category("grandchild", "child")], "root"), new Set(["child", "grandchild"]));
});
