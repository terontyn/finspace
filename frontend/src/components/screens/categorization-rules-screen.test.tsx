import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { apiClient } from "@/lib/api-client";
import type { CategorizationRule, CategorizationRulePage } from "@/types/categorization";
import type { Account, Category, Paged, Payee } from "@/types/finance";

import { CategorizationRulesScreen } from "./categorization-rules-screen";

const account: Account = { account_type: "debit_card", created_at: "2026-08-01T00:00:00Z", credit_limit: null, currency: "RUB", description: null, id: "account-1", institution: "Банк", is_archived: false, name: "Основной", opening_balance: "0.0000", opening_balance_at: "2026-08-01", updated_at: "2026-08-01T00:00:00Z", version: 1 };
const category: Category = { category_type: "expense", color: null, created_at: "2026-08-01T00:00:00Z", icon: null, id: "category-1", is_archived: false, name: "Кафе", parent_id: null, sort_order: 0, updated_at: "2026-08-01T00:00:00Z", version: 1 };
const payee: Payee = { aliases: [], created_at: "2026-08-01T00:00:00Z", deleted_at: null, id: "payee-1", name: "Кофейня", notes: null, updated_at: "2026-08-01T00:00:00Z", version: 1 };

function rule(overrides: Partial<CategorizationRule> = {}): CategorizationRule {
  return { account_id: "account-1", category_id: "category-1", counterparty_contains: "COFFEE", created_at: "2026-08-01T00:00:00Z", deleted_at: null, description_contains: null, id: "rule-1", is_active: true, name: "Кофе", payee_id: "payee-1", priority: 10, transaction_type: "expense", updated_at: "2026-08-01T00:00:00Z", version: 1, ...overrides };
}

function rulesPage(items: CategorizationRule[], offset = 0, total = items.length): CategorizationRulePage {
  return { items, page: { limit: 12, offset, total } };
}

function page<T>(items: T[]): Paged<T> {
  return { items, page: { limit: 200, offset: 0, total: items.length } };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await new Promise<void>((resolve) => setImmediate(resolve));
}

function renderedText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(renderedText).join("");
  if (value && typeof value === "object" && "children" in value) return renderedText((value as { children?: unknown }).children);
  return "";
}

function button(root: ReactTestInstance, label: string): ReactTestInstance {
  const match = root.findAllByType("button").find((node) => renderedText(node.props.children).includes(label));
  if (!match) throw new Error(`Button not found: ${label}`);
  return match;
}

function installBrowserGlobals(): () => void {
  const descriptors = new Map<string, PropertyDescriptor | undefined>();
  for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) descriptors.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  class TestHTMLElement { focus() {} }
  const browser = { addEventListener: () => undefined, cancelAnimationFrame: () => undefined, removeEventListener: () => undefined, requestAnimationFrame: (callback: () => void) => { callback(); return 1; } };
  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: TestHTMLElement });
  Object.defineProperty(globalThis, "document", { configurable: true, value: { activeElement: null } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: browser });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });
  return () => { for (const [key, descriptor] of descriptors) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key); } };
}

interface HarnessOptions {
  list?: (path: string) => CategorizationRulePage;
  onDelete?: (path: string) => CategorizationRule;
  onPatch?: (path: string, body: unknown) => CategorizationRule;
  onPost?: (path: string, body: unknown) => CategorizationRule;
  role?: "owner" | "editor" | "viewer" | null;
}

async function createHarness(options: HarnessOptions = {}) {
  const restoreBrowser = installBrowserGlobals();
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const originalPatch = apiClient.patch;
  const originalDelete = apiClient.delete;
  const calls: Array<{ body?: unknown; method: string; path: string }> = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    if (path.startsWith("/api/v1/categorization-rules?")) return Promise.resolve((options.list?.(path) ?? rulesPage([rule()])) as T);
    if (path.startsWith("/api/v1/accounts?")) return Promise.resolve(page([account]) as T);
    if (path.startsWith("/api/v1/categories?")) return Promise.resolve(page([category]) as T);
    if (path.startsWith("/api/v1/payees?")) return Promise.resolve(page([payee]) as T);
    throw new Error(`Unexpected GET ${path}`);
  }) as typeof apiClient.get;
  apiClient.post = (<T,>(path: string, body?: unknown) => { calls.push({ body, method: "POST", path }); return Promise.resolve((options.onPost?.(path, body) ?? rule()) as T); }) as typeof apiClient.post;
  apiClient.patch = (<T,>(path: string, body: unknown) => { calls.push({ body, method: "PATCH", path }); return Promise.resolve((options.onPatch?.(path, body) ?? rule({ version: 2 })) as T); }) as typeof apiClient.patch;
  apiClient.delete = (<T,>(path: string) => { calls.push({ method: "DELETE", path }); return Promise.resolve((options.onDelete?.(path) ?? rule({ deleted_at: "2026-08-02T00:00:00Z", version: 2 })) as T); }) as typeof apiClient.delete;
  await act(async () => { renderer = create(<CategorizationRulesScreen onError={(error) => { throw error; }} role={options.role === undefined ? "owner" : options.role} roleLoading={false}/>); await settle(); });
  if (!renderer) throw new Error("Renderer was not created");
  return { calls, renderer, async cleanup() { await act(async () => renderer?.unmount()); apiClient.get = originalGet; apiClient.post = originalPost; apiClient.patch = originalPatch; apiClient.delete = originalDelete; restoreBrowser(); } };
}

test("/rules renders real server data, readable AND conditions and priority precedence", async () => {
  const harness = await createHarness({ list: () => rulesPage([rule({ priority: 3 })]) });
  try {
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Кофе/);
    assert.match(text, /Правила с меньшим номером приоритета проверяются раньше/);
    assert.match(text, /Тип: Расход/);
    assert.match(text, /Счёт: Основной/);
    assert.match(text, /Получатель: Кофейня/);
    assert.match(text, /Контрагент содержит «COFFEE»/);
    assert.match(text, /Все условия одновременно/);
    assert.match(text, /Приоритет3/);
  } finally { await harness.cleanup(); }
});

test("viewer can list rules but has no create, edit, archive or restore controls", async () => {
  const archived = rule({ deleted_at: "2026-08-02T00:00:00Z" });
  const harness = await createHarness({ list: () => rulesPage([archived]), role: "viewer" });
  try {
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Режим просмотра/);
    assert.match(text, /Кофе/);
    const labels = harness.renderer.root.findAllByType("button").map((node) => renderedText(node.props.children));
    assert.equal(labels.some((label) => label.includes("Создать правило")), false);
    assert.equal(labels.includes("Изменить"), false);
    assert.equal(labels.includes("В архив"), false);
    assert.equal(labels.includes("Восстановить"), false);
  } finally { await harness.cleanup(); }
});

test("create validates a matcher and keeps canonical Payee separate from counterparty text", async () => {
  const harness = await createHarness();
  try {
    await act(async () => { button(harness.renderer.root, "＋ Создать правило").props.onClick(); });
    let dialog = harness.renderer.root.findByProps({ "aria-label": "Новое правило категоризации" });
    await act(async () => { dialog.findByProps({ maxLength: 200 }).props.onChange({ target: { value: "Новое правило" } }); });
    dialog = harness.renderer.root.findByProps({ "aria-label": "Новое правило категоризации" });
    await act(async () => { dialog.findByProps({ "aria-label": "Целевая категория" }).props.onChange({ target: { value: "category-1" } }); });
    await act(async () => { dialog.findByProps({ className: "categorization-rule-form" }).props.onSubmit({ preventDefault: () => undefined }); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /CATEGORIZATION_MATCHER_REQUIRED/);
    assert.equal(harness.calls.length, 0);

    dialog = harness.renderer.root.findByProps({ "aria-label": "Новое правило категоризации" });
    await act(async () => { dialog.findByProps({ "aria-label": "Получатель правила" }).props.onChange({ target: { value: "payee-1" } }); });
    dialog = harness.renderer.root.findByProps({ "aria-label": "Новое правило категоризации" });
    await act(async () => { dialog.findByProps({ placeholder: "Например, COFFEE SHOP" }).props.onChange({ target: { value: " COFFEE " } }); });
    await act(async () => { dialog.findByProps({ className: "categorization-rule-form" }).props.onSubmit({ preventDefault: () => undefined }); await settle(); });
    const call = harness.calls.find((item) => item.method === "POST" && item.path === "/api/v1/categorization-rules");
    assert.ok(call);
    assert.deepEqual(call.body, { account_id: null, category_id: "category-1", counterparty_contains: "COFFEE", description_contains: null, is_active: true, name: "Новое правило", payee_id: "payee-1", priority: 100, transaction_type: null });
  } finally { await harness.cleanup(); }
});

test("edit can remove an optional matcher and archive/restore use current versions", async () => {
  let current = rule();
  const harness = await createHarness({
    list: (path) => rulesPage(path.includes("include_deleted=true") || !current.deleted_at ? [current] : []),
    onDelete: () => (current = { ...current, deleted_at: "2026-08-02T00:00:00Z", version: current.version + 1 }),
    onPatch: (_path, body) => { const record = body as { payee_id: string | null }; current = { ...current, payee_id: record.payee_id, version: current.version + 1 }; return current; },
    onPost: (path) => { if (!path.endsWith("/restore")) return current; current = { ...current, deleted_at: null, version: current.version + 1 }; return current; },
  });
  try {
    await act(async () => { button(harness.renderer.root.findByProps({ "data-rule-id": "rule-1" }), "Изменить").props.onClick(); });
    let dialog = harness.renderer.root.findByProps({ "aria-label": "Редактирование правила категоризации" });
    await act(async () => { dialog.findByProps({ "aria-label": "Получатель правила" }).props.onChange({ target: { value: "" } }); });
    await act(async () => { dialog.findByProps({ className: "categorization-rule-form" }).props.onSubmit({ preventDefault: () => undefined }); await settle(); });
    const patchCall = harness.calls.find((item) => item.method === "PATCH");
    assert.equal((patchCall?.body as { payee_id?: unknown }).payee_id, null);
    assert.equal((patchCall?.body as { version?: unknown }).version, 1);

    await act(async () => { button(harness.renderer.root.findByProps({ "data-rule-id": "rule-1" }), "В архив").props.onClick(); await settle(); });
    assert.equal(harness.calls.find((item) => item.method === "DELETE")?.path, "/api/v1/categorization-rules/rule-1?version=2");
    const archiveToggle = harness.renderer.root.findAllByType("input").find((node) => node.props.type === "checkbox");
    assert.ok(archiveToggle);
    await act(async () => { archiveToggle.props.onChange({ target: { checked: true } }); await settle(); });
    await act(async () => { button(harness.renderer.root.findByProps({ "data-rule-id": "rule-1" }), "Восстановить").props.onClick(); await settle(); });
    const restoreCall = harness.calls.find((item) => item.method === "POST" && item.path.endsWith("/restore"));
    assert.deepEqual(restoreCall, { body: { version: 3 }, method: "POST", path: "/api/v1/categorization-rules/rule-1/restore" });
  } finally { await harness.cleanup(); }
});

test("archiving the final rule on a page returns to the last valid backend page", async () => {
  const firstPage = Array.from({ length: 12 }, (_, index) => rule({ id: `rule-${index}`, name: `Правило ${index}` }));
  const finalRule = rule({ id: "rule-final", name: "Последнее правило" });
  let archived = false;
  const paths: string[] = [];
  const harness = await createHarness({
    list: (path) => {
      paths.push(path);
      const offset = new URLSearchParams(path.split("?")[1]).get("offset");
      if (offset === "12") return rulesPage(archived ? [] : [finalRule], 12, archived ? 12 : 13);
      return rulesPage(firstPage, 0, archived ? 12 : 13);
    },
    onDelete: () => { archived = true; return { ...finalRule, deleted_at: "2026-08-02T00:00:00Z", version: 2 }; },
  });
  try {
    await act(async () => { button(harness.renderer.root, "Дальше").props.onClick(); await settle(); });
    assert.ok(harness.renderer.root.findByProps({ "data-rule-id": "rule-final" }));
    await act(async () => { button(harness.renderer.root.findByProps({ "data-rule-id": "rule-final" }), "В архив").props.onClick(); await settle(); });
    assert.equal(paths.some((path) => path.includes("offset=12")), true);
    assert.equal(paths.at(-1)?.includes("offset=0"), true);
    assert.match(renderedText(harness.renderer.toJSON()), /1–12 из 12/);
  } finally { await harness.cleanup(); }
});
