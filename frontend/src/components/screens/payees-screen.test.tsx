import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { apiClient } from "@/lib/api-client";
import type { Payee } from "@/types/finance";

import { PayeesScreen } from "./payees-screen";

Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });

const primaryAlias = { id: "alias-primary", alias: "Кофейня", is_primary: true, created_at: "2026-08-30T00:00:00Z", updated_at: "2026-08-30T00:00:00Z", deleted_at: null };
const secondaryAlias = { id: "alias-secondary", alias: "COFFEE SHOP", is_primary: false, created_at: "2026-08-30T00:00:00Z", updated_at: "2026-08-30T00:00:00Z", deleted_at: null };
const activePayee: Payee = {
  aliases: [primaryAlias, secondaryAlias],
  created_at: "2026-08-30T00:00:00Z",
  deleted_at: null,
  id: "payee-1",
  name: "Кофейня",
  notes: "Утренний кофе",
  updated_at: "2026-08-30T00:00:00Z",
  version: 1,
};

function installBrowserGlobals(): () => void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      addEventListener: () => undefined,
      cancelAnimationFrame: () => undefined,
      removeEventListener: () => undefined,
      requestAnimationFrame: (callback: () => void) => { callback(); return 1; },
    },
  });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });
  return () => {
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
    else Reflect.deleteProperty(globalThis, "window");
    Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
  };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function nodeText(node: ReactTestInstance): string {
  return node.children.map((child) => typeof child === "string" ? child : nodeText(child)).join("");
}

function button(node: ReactTestInstance, label: string): ReactTestInstance {
  const found = node.findAllByType("button").find((item) => nodeText(item).includes(label));
  if (!found) throw new Error(`Button not found: ${label}`);
  return found;
}

function paged(items: Payee[]) {
  return { items, page: { limit: 12, offset: 0, total: items.length } };
}

test("PayeesScreen exposes initial loading and API failure states", async () => {
  const originalGet = apiClient.get;
  const errors: unknown[] = [];
  let resolveRequest: ((value: ReturnType<typeof paged>) => void) | undefined;
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>() => new Promise<T>((resolve) => { resolveRequest = (value) => resolve(value as T); })) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<PayeesScreen onError={(error) => errors.push(error)} role="owner" roleLoading={false}/>); });
    assert.ok(renderer);
    assert.ok(renderer.root.findByProps({ "aria-label": "Загружаем получателей" }));
    await act(async () => { resolveRequest?.(paged([])); await settle(); });
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
  }

  const failure = new Error("offline");
  apiClient.get = (() => Promise.reject(failure)) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<PayeesScreen onError={(error) => errors.push(error)} role="owner" roleLoading={false}/>); await settle(); });
    assert.ok(renderer);
    assert.ok(renderer.root.findByProps({ className: "empty-state payee-load-error" }));
    assert.equal(errors.includes(failure), true);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
  }
});

test("PayeesScreen distinguishes empty, populated and server-backed search states", async () => {
  const originalGet = apiClient.get;
  const paths: string[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    paths.push(path);
    const result = path.includes("search=missing") ? paged([]) : paged(path.includes("search=") ? [activePayee] : []);
    return Promise.resolve(result as T);
  }) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<PayeesScreen onError={(error) => { throw error; }} role="owner" roleLoading={false}/>); await settle(); });
    assert.ok(renderer);
    assert.equal(nodeText(renderer.root).includes("Получателей пока нет"), true);

    const search = renderer.root.findByProps({ "aria-label": "Поиск получателей" });
    const searchForm = renderer.root.findByProps({ className: "payee-filters" });
    await act(async () => { search.props.onChange({ target: { value: "missing" } }); });
    await act(async () => { searchForm.props.onSubmit({ preventDefault: () => undefined }); await settle(); });
    assert.equal(paths.some((path) => path.includes("search=missing")), true);
    assert.equal(nodeText(renderer.root).includes("Получатели не найдены"), true);

    await act(async () => { search.props.onChange({ target: { value: "COFFEE" } }); });
    await act(async () => { searchForm.props.onSubmit({ preventDefault: () => undefined }); await settle(); });
    assert.equal(nodeText(renderer.root).includes("Кофейня"), true);
    assert.equal(nodeText(renderer.root).includes("COFFEE SHOP"), true);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
  }
});

test("viewer can search and inspect Payees but has no write controls", async () => {
  const originalGet = apiClient.get;
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>() => Promise.resolve(paged([activePayee]) as T)) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<PayeesScreen onError={(error) => { throw error; }} role="viewer" roleLoading={false}/>); await settle(); });
    assert.ok(renderer);
    assert.equal(nodeText(renderer.root).includes("Режим просмотра"), true);
    assert.equal(nodeText(renderer.root).includes("Кофейня"), true);
    const labels = renderer.root.findAllByType("button").map(nodeText);
    assert.equal(labels.some((label) => label.includes("Получатель")), false);
    assert.equal(labels.includes("Изменить"), false);
    assert.equal(labels.includes("В архив"), false);
    assert.equal(labels.includes("Восстановить"), false);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
  }
});

test("owner Payee lifecycle and alias actions send current versions", async () => {
  const restoreWindow = installBrowserGlobals();
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const originalPatch = apiClient.patch;
  const originalDelete = apiClient.delete;
  const calls: Array<{ method: string; path: string; body?: unknown }> = [];
  let serverItems: Payee[] = [activePayee];
  let renderer: ReactTestRenderer | undefined;

  apiClient.get = (<T,>(path: string) => {
    const visible = path.includes("include_deleted=true") ? serverItems : serverItems.filter((item) => !item.deleted_at);
    return Promise.resolve(paged(visible) as T);
  }) as typeof apiClient.get;
  apiClient.post = (<T,>(path: string, body?: unknown) => {
    calls.push({ method: "POST", path, body });
    if (path === "/api/v1/payees") {
      const created = { ...activePayee, id: "payee-created", name: "Новый", aliases: [{ ...primaryAlias, id: "created-primary", alias: "Новый" }] };
      serverItems = [...serverItems, created];
      return Promise.resolve(created as T);
    }
    const current = serverItems[0];
    if (path.endsWith("/aliases")) {
      const updated = { ...current, aliases: [...current.aliases, { ...secondaryAlias, id: "alias-new", alias: "CAFE" }], version: current.version + 1 };
      serverItems[0] = updated;
      return Promise.resolve(updated as T);
    }
    if (path.includes("/aliases/") && path.endsWith("/restore")) {
      const aliasId = path.split("/").at(-2);
      const updated = { ...current, aliases: current.aliases.map((alias) => alias.id === aliasId ? { ...alias, deleted_at: null } : alias), version: current.version + 1 };
      serverItems[0] = updated;
      return Promise.resolve(updated as T);
    }
    if (path.endsWith("/restore")) {
      const updated = { ...current, deleted_at: null, version: current.version + 1 };
      serverItems[0] = updated;
      return Promise.resolve(updated as T);
    }
    throw new Error(`Unexpected POST ${path}`);
  }) as typeof apiClient.post;
  apiClient.patch = (<T,>(path: string, body: unknown) => {
    calls.push({ method: "PATCH", path, body });
    const current = serverItems.find((item) => path.endsWith(item.id));
    if (!current) throw new Error(`Unexpected PATCH ${path}`);
    const record = body as { name: string; notes: string | null; version: number };
    const updated = { ...current, name: record.name, notes: record.notes, version: current.version + 1 };
    serverItems = serverItems.map((item) => item.id === updated.id ? updated : item);
    return Promise.resolve(updated as T);
  }) as typeof apiClient.patch;
  apiClient.delete = (<T,>(path: string) => {
    calls.push({ method: "DELETE", path });
    const current = serverItems[0];
    if (path.includes("/aliases/")) {
      const aliasId = path.split("/aliases/")[1].split("?")[0];
      const updated = { ...current, aliases: current.aliases.map((alias) => alias.id === aliasId ? { ...alias, deleted_at: "2026-08-30T01:00:00Z" } : alias), version: current.version + 1 };
      serverItems[0] = updated;
      return Promise.resolve(updated as T);
    }
    const updated = { ...current, deleted_at: "2026-08-30T02:00:00Z", version: current.version + 1 };
    serverItems[0] = updated;
    return Promise.resolve(updated as T);
  }) as typeof apiClient.delete;

  try {
    await act(async () => { renderer = create(<PayeesScreen onError={(error) => { throw error; }} role="owner" roleLoading={false}/>); await settle(); });
    assert.ok(renderer);
    const view = renderer;

    await act(async () => { button(view.root, "＋ Получатель").props.onClick(); });
    let dialog = view.root.findByProps({ "aria-label": "Новый получатель" });
    await act(async () => { dialog.findByType("input").props.onChange({ target: { value: "Новый" } }); });
    await act(async () => { dialog.findByProps({ className: "entity-form" }).props.onSubmit({ preventDefault: () => undefined }); await settle(); });
    assert.deepEqual(calls.find((call) => call.method === "POST" && call.path === "/api/v1/payees")?.body, { name: "Новый", notes: null });

    const row = view.root.findByProps({ "data-payee-id": "payee-1" });
    await act(async () => { button(row, "Изменить").props.onClick(); });
    dialog = view.root.findByProps({ "aria-label": "Редактирование получателя" });
    const aliasInput = dialog.findByProps({ placeholder: "Например, название из выписки" });
    await act(async () => { aliasInput.props.onChange({ target: { value: "CAFE" } }); });
    await act(async () => { dialog.findByProps({ className: "payee-alias-create" }).props.onSubmit({ preventDefault: () => undefined }); await settle(); });
    assert.deepEqual(calls.at(-1), { method: "POST", path: "/api/v1/payees/payee-1/aliases", body: { alias: "CAFE", version: 1 } });

    dialog = view.root.findByProps({ "aria-label": "Редактирование получателя" });
    await act(async () => { button(dialog.findAllByProps({ className: "payee-alias-list" })[0], "В архив").props.onClick(); await settle(); });
    assert.equal(calls.at(-1)?.path, "/api/v1/payees/payee-1/aliases/alias-secondary?version=2");
    dialog = view.root.findByProps({ "aria-label": "Редактирование получателя" });
    await act(async () => { button(dialog.findByProps({ className: "payee-alias-list" }), "Восстановить").props.onClick(); await settle(); });
    assert.deepEqual(calls.at(-1), { method: "POST", path: "/api/v1/payees/payee-1/aliases/alias-secondary/restore", body: { version: 3 } });

    dialog = view.root.findByProps({ "aria-label": "Редактирование получателя" });
    const nameInput = dialog.findByProps({ value: "Кофейня" });
    await act(async () => { nameInput.props.onChange({ target: { value: "Кофейня 2" } }); });
    await act(async () => { dialog.findByProps({ className: "entity-form" }).props.onSubmit({ preventDefault: () => undefined }); await settle(); });
    assert.deepEqual(calls.at(-1), { method: "PATCH", path: "/api/v1/payees/payee-1", body: { name: "Кофейня 2", notes: "Утренний кофе", version: 4 } });

    await act(async () => { button(view.root.findByProps({ "data-payee-id": "payee-1" }), "В архив").props.onClick(); await settle(); });
    assert.equal(calls.at(-1)?.path, "/api/v1/payees/payee-1?version=5");
    const archiveToggle = view.root.findByProps({ type: "checkbox" });
    await act(async () => { archiveToggle.props.onChange({ target: { checked: true } }); await settle(); });
    await act(async () => { button(view.root.findByProps({ "data-payee-id": "payee-1" }), "Восстановить").props.onClick(); await settle(); });
    assert.deepEqual(calls.at(-1), { method: "POST", path: "/api/v1/payees/payee-1/restore", body: { version: 6 } });
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    apiClient.patch = originalPatch;
    apiClient.delete = originalDelete;
    restoreWindow();
  }
});

test("PayeesScreen returns to the last valid page after archiving the final row", async () => {
  const restoreWindow = installBrowserGlobals();
  const originalGet = apiClient.get;
  const originalDelete = apiClient.delete;
  const paths: string[] = [];
  let archived = false;
  let renderer: ReactTestRenderer | undefined;
  const firstPage = Array.from({ length: 12 }, (_, index): Payee => ({
    ...activePayee,
    aliases: [{ ...primaryAlias, id: `alias-${index + 1}`, alias: `Получатель ${index + 1}` }],
    id: `payee-${index + 1}`,
    name: `Получатель ${index + 1}`,
  }));
  const finalPayee: Payee = {
    ...activePayee,
    aliases: [{ ...primaryAlias, id: "alias-13", alias: "Получатель 13" }],
    id: "payee-13",
    name: "Получатель 13",
  };

  apiClient.get = (<T,>(path: string) => {
    paths.push(path);
    const params = new URLSearchParams(path.split("?")[1] ?? "");
    const requestedOffset = Number(params.get("offset") ?? "0");
    if (!archived) {
      const items = requestedOffset === 12 ? [finalPayee] : firstPage;
      return Promise.resolve({ items, page: { limit: 12, offset: requestedOffset, total: 13 } } as T);
    }
    const items = requestedOffset === 12 ? [] : firstPage;
    return Promise.resolve({ items, page: { limit: 12, offset: requestedOffset, total: 12 } } as T);
  }) as typeof apiClient.get;
  apiClient.delete = (<T,>() => {
    archived = true;
    return Promise.resolve({ ...finalPayee, deleted_at: "2026-08-30T03:00:00Z", version: 2 } as T);
  }) as typeof apiClient.delete;

  try {
    await act(async () => { renderer = create(<PayeesScreen onError={(error) => { throw error; }} role="owner" roleLoading={false}/>); await settle(); });
    assert.ok(renderer);
    const view = renderer;

    await act(async () => { button(view.root, "Дальше").props.onClick(); await settle(); });
    assert.ok(view.root.findByProps({ "data-payee-id": "payee-13" }));

    await act(async () => {
      button(view.root.findByProps({ "data-payee-id": "payee-13" }), "В архив").props.onClick();
      await settle();
      await settle();
    });

    assert.ok(view.root.findByProps({ "data-payee-id": "payee-1" }));
    assert.equal(nodeText(view.root).includes("1–12 из 12"), true);
    assert.equal(nodeText(view.root).includes("13–12 из 12"), false);
    assert.equal(paths.filter((path) => path.includes("offset=12")).length >= 2, true);
    assert.equal(paths.at(-1)?.includes("offset=0"), true);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.delete = originalDelete;
    restoreWindow();
  }
});
