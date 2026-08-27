import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { ApiClientError, apiClient } from "@/lib/api-client";
import type { BudgetGroup, BudgetMonth, BudgetRevisionPage } from "@/types/budget";
import type { Category, Paged } from "@/types/finance";

import { BudgetScreen } from "./budget-screen";

const categoryBase = {
  color: "#00d68f",
  created_at: "2026-01-01T00:00:00Z",
  icon: "•",
  is_archived: false,
  sort_order: 0,
  updated_at: "2026-01-01T00:00:00Z",
  version: 1,
};

const categories: Category[] = [
  { ...categoryBase, category_type: "expense", id: "category-parent", name: "Дом", parent_id: null },
  { ...categoryBase, category_type: "expense", id: "category-child", name: "Коммунальные услуги", parent_id: "category-parent", sort_order: 1 },
  { ...categoryBase, category_type: "both", id: "category-both", name: "Корректировки", parent_id: null, sort_order: 2 },
  { ...categoryBase, category_type: "income", id: "category-income", name: "Зарплата", parent_id: null, sort_order: 3 },
  { ...categoryBase, category_type: "expense", id: "category-archived", is_archived: true, name: "Старое", parent_id: null, sort_order: 4 },
];

function budgetGroup(overrides: Partial<BudgetGroup> = {}): BudgetGroup {
  return {
    actual_expense: "125.0000",
    actual_income: "1000.0000",
    actual_net_cashflow: "875.0000",
    adjustment: "5.0000",
    allocated: "200.0000",
    allocations: [
      {
        actual: "25.0000",
        category_archived: false,
        category_deleted: false,
        category_id: "category-parent",
        category_name: "Дом",
        category_type: "expense",
        id: "allocation-parent",
        note: "Родительская точная категория",
        parent_id: null,
        planned: "100.0000",
        remaining: "75.0000",
        usage_percent: "25.0000",
      },
      {
        actual: "125.0000",
        category_archived: false,
        category_deleted: false,
        category_id: "category-child",
        category_name: "Коммунальные услуги",
        category_type: "expense",
        id: "allocation-child",
        note: null,
        parent_id: "category-parent",
        planned: "100.0000",
        remaining: "-25.0000",
        usage_percent: "125.0000",
      },
    ],
    budgeted_actual_expense: "100.0000",
    currency: "RUB",
    deleted_at: null,
    frozen: false,
    id: "budget-rub",
    period: "2026-08",
    planned_income: "1000.0000",
    planning_capacity: "1050.0000",
    projection_source: "live",
    remaining: "-25.0000",
    rollover: { amount: "50.0000", provisional: true, source_policy: "full" },
    rollover_policy: "positive_only",
    unallocated: "850.0000",
    unbudgeted_actual_expense: "25.0000",
    version: 2,
    workspace_id: "workspace-1",
    ...overrides,
  };
}

function budgetMonth(groups: BudgetGroup[]): BudgetMonth {
  return {
    groups,
    historical_snapshot_available: true,
    period: "2026-08",
    projection_source: groups[0]?.projection_source ?? "live",
    timezone: "Asia/Yekaterinburg",
  };
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

function button(renderer: ReactTestRenderer, label: string, last = false): ReactTestInstance {
  const matches = renderer.root.findAllByType("button").filter((node) => renderedText(node.props.children) === label);
  const match = last ? matches.at(-1) : matches[0];
  if (!match) throw new Error(`Button not found: ${label}`);
  return match;
}

function installBrowserGlobals(): () => void {
  const descriptors = new Map<string, PropertyDescriptor | undefined>();
  for (const key of ["window", "self", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) descriptors.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  class TestHTMLElement { focus() {} }
  const browser = {
    addEventListener: () => undefined,
    cancelAnimationFrame: () => undefined,
    removeEventListener: () => undefined,
    requestAnimationFrame: (callback: () => void) => { callback(); return 1; },
  };
  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: TestHTMLElement });
  Object.defineProperty(globalThis, "document", { configurable: true, value: { activeElement: null } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: browser });
  Object.defineProperty(globalThis, "self", { configurable: true, value: browser });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });
  return () => {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else Reflect.deleteProperty(globalThis, key);
    }
  };
}

interface HarnessOptions {
  groups?: BudgetGroup[];
  history?: (path: string) => Promise<BudgetRevisionPage>;
  month?: (read: number, path: string) => BudgetMonth | Promise<BudgetMonth>;
  onRequest?: (path: string, init: RequestInit) => Promise<unknown>;
  preferredCurrency?: string;
  role?: string;
}

async function createHarness(options: HarnessOptions = {}) {
  const originalGet = apiClient.get;
  const originalRequest = apiClient.request;
  const restoreBrowser = installBrowserGlobals();
  const errors: unknown[] = [];
  const budgetPaths: string[] = [];
  let budgetRead = 0;
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    if (path.startsWith("/api/v1/budgets/") && path.includes("/history?")) {
      return (options.history?.(path) ?? Promise.resolve({ items: [], page: { limit: 20, offset: 0, total: 0 } })) as Promise<T>;
    }
    if (path.startsWith("/api/v1/budgets/")) {
      budgetPaths.push(path);
      budgetRead += 1;
      return Promise.resolve(options.month?.(budgetRead, path) ?? budgetMonth(options.groups ?? [])) as Promise<T>;
    }
    if (path.startsWith("/api/v1/categories?")) return Promise.resolve({ items: categories, page: { limit: 500, offset: 0, total: categories.length } } as Paged<Category> as T);
    if (path === "/api/v1/auth/me") return Promise.resolve({ role: options.role ?? "owner" } as T);
    throw new Error(`Unexpected GET ${path}`);
  }) as typeof apiClient.get;
  apiClient.request = (<T,>(path: string, init: RequestInit = {}) => {
    if (!options.onRequest) throw new Error(`Unexpected mutation ${init.method} ${path}`);
    return options.onRequest(path, init) as Promise<T>;
  }) as typeof apiClient.request;
  await act(async () => {
    renderer = create(<BudgetScreen onError={(error) => errors.push(error)} preferredCurrency={options.preferredCurrency ?? "RUB"} timezone="Asia/Yekaterinburg"/>);
    await settle();
  });
  if (!renderer) throw new Error("Budget renderer was not created");
  return {
    budgetPaths,
    errors,
    renderer,
    async cleanup() {
      await act(async () => renderer?.unmount());
      apiClient.get = originalGet;
      apiClient.request = originalRequest;
      restoreBrowser();
    },
  };
}

test("empty month shows honest create/copy actions and viewer remains read-only", async () => {
  const owner = await createHarness();
  try {
    const text = renderedText(owner.renderer.toJSON());
    assert.match(text, /Бюджет на август 2026 г\. ещё не создан/);
    assert.match(text, /Создать бюджет/);
    assert.match(text, /Скопировать прошлый месяц/);
    assert.equal(owner.budgetPaths[0], "/api/v1/budgets/2026-08?include_deleted=true");
  } finally { await owner.cleanup(); }

  const viewer = await createHarness({ role: "viewer" });
  try {
    const text = renderedText(viewer.renderer.toJSON());
    assert.match(text, /Роль viewer/);
    assert.doesNotMatch(text, /Создать бюджет/);
  } finally { await viewer.cleanup(); }

  const editor = await createHarness({ role: "editor" });
  try {
    const text = renderedText(editor.renderer.toJSON());
    assert.match(text, /Создать бюджет/);
    assert.match(text, /Скопировать прошлый месяц/);
    assert.doesNotMatch(text, /Роль viewer/);
    await act(async () => { button(editor.renderer, "Создать бюджет").props.onClick(); });
    assert.ok(editor.renderer.root.findByProps({ "aria-label": "Создание бюджета" }));
  } finally { await editor.cleanup(); }
});

test("create retries one network command with the same idempotency key and renders backend response", async () => {
  const requests: Array<{ body: Record<string, unknown>; key: string; path: string }> = [];
  const created = budgetGroup({ allocations: [], id: "created", version: 7 });
  const harness = await createHarness({
    onRequest: async (path, init) => {
      requests.push({ body: JSON.parse(String(init.body)), key: new Headers(init.headers).get("X-Idempotency-Key") ?? "", path });
      if (requests.length === 1) throw new ApiClientError("Network unavailable", "API_NETWORK_ERROR", 0);
      return created;
    },
  });
  try {
    await act(async () => { button(harness.renderer, "Создать бюджет").props.onClick(); });
    const drawer = harness.renderer.root.findByProps({ "aria-label": "Создание бюджета" });
    const inputs = drawer.findAllByType("input");
    await act(async () => { inputs[1].props.onChange({ target: { value: "1000" } }); });
    const form = drawer.findByType("form");
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /Network unavailable/);
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(requests.length, 2);
    assert.equal(requests[0].key, requests[1].key);
    assert.ok(requests[0].key.length > 10);
    assert.equal(requests[0].path, "/api/v1/budgets/2026-08/RUB");
    assert.deepEqual(requests[0].body, { allocations: [], planned_income: "1000", rollover_policy: "none" });
    assert.match(renderedText(harness.renderer.toJSON()), /v7/);
  } finally { await harness.cleanup(); }
});

test("editing a rejected command starts a new semantic intent with a new idempotency key", async () => {
  const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
  const harness = await createHarness({ onRequest: async (_path, init) => {
    requests.push({ body: JSON.parse(String(init.body)), key: new Headers(init.headers).get("X-Idempotency-Key") ?? "" });
    if (requests.length === 1) throw new ApiClientError("Invalid allocation", "BUDGET_ALLOCATION_INVALID", 422);
    return budgetGroup({ allocations: [], version: 1 });
  } });
  try {
    await act(async () => { button(harness.renderer, "Создать бюджет").props.onClick(); });
    const drawer = harness.renderer.root.findByProps({ "aria-label": "Создание бюджета" });
    const income = drawer.findAllByType("input")[1];
    await act(async () => { income.props.onChange({ target: { value: "1000" } }); });
    await act(async () => { drawer.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    await act(async () => { income.props.onChange({ target: { value: "1100" } }); });
    await act(async () => { drawer.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(requests.length, 2);
    assert.notEqual(requests[0].key, requests[1].key);
    assert.equal(requests[0].body.planned_income, "1000");
    assert.equal(requests[1].body.planned_income, "1100");
  } finally { await harness.cleanup(); }
});

test("edit sends current version and complete allocations, then trusts response version", async () => {
  let captured: { body: Record<string, unknown>; path: string } | null = null;
  const result = budgetGroup({ planned_income: "1200.0000", version: 9 });
  const harness = await createHarness({ groups: [budgetGroup()], onRequest: async (path, init) => {
    captured = { body: JSON.parse(String(init.body)), path };
    return result;
  } });
  try {
    await act(async () => { button(harness.renderer, "Изменить план").props.onClick(); });
    let drawer = harness.renderer.root.findByProps({ "aria-label": "Редактирование бюджета" });
    let inputs = drawer.findAllByType("input");
    await act(async () => { inputs[1].props.onChange({ target: { value: "1200" } }); });
    await act(async () => { drawer.findByProps({ "aria-label": "Закрыть" }).props.onClick(); });
    await act(async () => { button(harness.renderer, "Изменить план").props.onClick(); });
    drawer = harness.renderer.root.findByProps({ "aria-label": "Редактирование бюджета" });
    inputs = drawer.findAllByType("input");
    assert.equal(inputs[1].props.value, "1000.0000");
    await act(async () => { inputs[1].props.onChange({ target: { value: "1200" } }); });
    await act(async () => { drawer.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.ok(captured);
    const body = (captured as { body: Record<string, unknown> }).body;
    assert.equal(body.version, 2);
    assert.equal((body.allocations as unknown[]).length, 2);
    assert.deepEqual((body.allocations as Array<Record<string, unknown>>).map((item) => item.category_id), ["category-parent", "category-child"]);
    assert.match(renderedText(harness.renderer.toJSON()), /v9/);
  } finally { await harness.cleanup(); }
});

test("an existing unavailable allocation stays visible and is not silently dropped from aggregate PUT", async () => {
  let body: Record<string, unknown> | null = null;
  const unavailable = {
    ...budgetGroup().allocations[0],
    category_archived: true,
    category_id: "category-archived",
    category_name: "Старая категория",
    id: "allocation-archived",
  };
  const group = budgetGroup({ allocations: [unavailable] });
  const harness = await createHarness({ groups: [group], onRequest: async (_path, init) => {
    body = JSON.parse(String(init.body));
    return budgetGroup({ allocations: [unavailable], version: 3 });
  } });
  try {
    assert.match(renderedText(harness.renderer.toJSON()), /Категория больше недоступна/);
    await act(async () => { button(harness.renderer, "Изменить план").props.onClick(); });
    const drawer = harness.renderer.root.findByProps({ "aria-label": "Редактирование бюджета" });
    assert.match(renderedText(drawer), /Старая категория · недоступна/);
    await act(async () => { drawer.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.ok(body);
    assert.deepEqual((body as { allocations: Array<{ category_id: string }> }).allocations.map((item) => item.category_id), ["category-archived"]);
  } finally { await harness.cleanup(); }
});

test("currency tabs never create a mixed total and switch independent projections", async () => {
  const usd = budgetGroup({ allocations: [{ ...budgetGroup().allocations[0], category_name: "Housing USD", id: "usd-allocation" }], currency: "USD", id: "budget-usd", planned_income: "25.0000", version: 3 });
  const harness = await createHarness({ groups: [budgetGroup(), usd] });
  try {
    const initial = renderedText(harness.renderer.toJSON());
    assert.match(initial, /RUB/);
    assert.match(initial, /USD/);
    assert.doesNotMatch(initial, /Общий итог|Grand total/);
    const usdTab = harness.renderer.root.findAllByProps({ role: "tab" }).find((node) => node.findByType("strong").children.includes("USD"));
    assert.ok(usdTab);
    await act(async () => { usdTab.props.onClick(); });
    const selected = renderedText(harness.renderer.toJSON());
    assert.match(selected, /Housing USD/);
    assert.doesNotMatch(selected, /Коммунальные услуги/);
  } finally { await harness.cleanup(); }

  const fallback = await createHarness({ groups: [usd, budgetGroup()], preferredCurrency: "EUR" });
  try {
    assert.match(renderedText(fallback.renderer.toJSON()), /Housing USD/);
    assert.doesNotMatch(renderedText(fallback.renderer.toJSON()), /Коммунальные услуги/);
  } finally { await fallback.cleanup(); }
});

test("rollover directions, exact parent-child rows, overspend and unbudgeted expense remain distinct", async () => {
  const harness = await createHarness({ groups: [budgetGroup({ rollover_policy: "none" })] });
  try {
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Перенос лимита из прошлого месяца/);
    assert.match(text, /Политика предыдущего плана: Полный остаток, включая перерасход/);
    assert.match(text, /Переносить остаток этого месяца/);
    assert.match(text, /Не переносить/);
    assert.match(text, /Предварительный перенос/);
    assert.match(text, /Дом/);
    assert.match(text, /Коммунальные услуги/);
    assert.match(text, /125\.0000% · перерасход/);
    assert.match(text, /Расходы вне распределений/);
    assert.ok(harness.renderer.root.findAllByProps({ "data-label": "План" }).length >= 2);
    const progress = harness.renderer.root.findAllByProps({ role: "progressbar" });
    assert.ok(progress.length >= 2);
    const over = progress.find((node) => String(node.props["aria-valuetext"]).includes("125.0000%"));
    assert.ok(over);
    assert.equal(over.props["aria-valuenow"], 100);
    assert.match(String(over.props["aria-valuetext"]), /перерасход/);
  } finally { await harness.cleanup(); }
});

test("out-of-order month responses cannot replace the newly selected period", async () => {
  let resolveStale: ((value: BudgetMonth) => void) | null = null;
  const stale = new Promise<BudgetMonth>((resolve) => { resolveStale = resolve; });
  const september = budgetGroup({
    allocations: [{ ...budgetGroup().allocations[0], category_name: "Сентябрьская категория", id: "september-allocation" }],
    id: "budget-september",
    period: "2026-09",
  });
  const harness = await createHarness({
    month: (read) => {
      if (read === 1) return budgetMonth([budgetGroup()]);
      if (read === 2) return stale;
      return { ...budgetMonth([september]), period: "2026-09" };
    },
  });
  try {
    const monthInput = harness.renderer.root.findByProps({ "aria-label": "Период бюджета" });
    await act(async () => { button(harness.renderer, "Обновить").props.onClick(); await Promise.resolve(); });
    await act(async () => { monthInput.props.onChange({ target: { value: "2026-09" } }); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /Сентябрьская категория/);
    await act(async () => { resolveStale?.(budgetMonth([budgetGroup({ id: "stale-august" })])); await settle(); });
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Сентябрьская категория/);
    assert.match(text, /сентябрь 2026 г\./i);
    assert.doesNotMatch(text, /Коммунальные услуги/);
  } finally { await harness.cleanup(); }
});

test("copy creates an empty target, confirms overwrite, and reports category conflict", async () => {
  const requests: Array<{ body: Record<string, unknown>; path: string }> = [];
  const copied = budgetGroup({ id: "copied", version: 1 });
  const empty = await createHarness({ onRequest: async (path, init) => {
    requests.push({ body: JSON.parse(String(init.body)), path });
    return copied;
  } });
  try {
    await act(async () => { button(empty.renderer, "Скопировать прошлый месяц").props.onClick(); await settle(); });
    assert.equal(requests[0].path, "/api/v1/budgets/2026-08/RUB/copy");
    assert.deepEqual(requests[0].body, { overwrite: false });
  } finally { await empty.cleanup(); }

  requests.length = 0;
  const existing = await createHarness({ groups: [budgetGroup()], onRequest: async (path, init) => {
    requests.push({ body: JSON.parse(String(init.body)), path });
    if (requests.length === 1) throw new ApiClientError("Conflicted categories", "BUDGET_COPY_CATEGORY_CONFLICT", 409, undefined, { category_ids: ["category-child"] });
    return copied;
  } });
  try {
    await act(async () => { button(existing.renderer, "Скопировать прошлый месяц с заменой").props.onClick(); });
    assert.match(renderedText(existing.renderer.toJSON()), /Заменить план RUB/);
    await act(async () => { button(existing.renderer, "Скопировать и заменить").props.onClick(); await settle(); });
    assert.deepEqual(requests[0].body, { overwrite: true, version: 2 });
    assert.match(renderedText(existing.renderer.toJSON()), /категории прошлого плана/);
  } finally { await existing.cleanup(); }
});

test("soft delete confirmation preserves transaction wording and restore sends returned version", async () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];
  const deleted = budgetGroup({ deleted_at: "2026-08-27T10:00:00Z", version: 3 });
  const restored = budgetGroup({ deleted_at: null, version: 4 });
  const harness = await createHarness({ groups: [budgetGroup()], onRequest: async (path, init) => {
    requests.push({ body: init.body ? JSON.parse(String(init.body)) : null, method: String(init.method), path });
    return init.method === "DELETE" ? deleted : restored;
  } });
  try {
    await act(async () => { button(harness.renderer, "Удалить план бюджета").props.onClick(); });
    assert.match(renderedText(harness.renderer.toJSON()), /Операции, категории и фактические расходы не удаляются/);
    await act(async () => { button(harness.renderer, "Удалить план бюджета", true).props.onClick(); await settle(); });
    assert.deepEqual(requests[0], { body: null, method: "DELETE", path: "/api/v1/budgets/2026-08/RUB?version=2" });
    assert.match(renderedText(harness.renderer.toJSON()), /soft delete/i);
    await act(async () => { button(harness.renderer, "Восстановить бюджет").props.onClick(); await settle(); });
    assert.deepEqual(requests[1], { body: { version: 3 }, method: "POST", path: "/api/v1/budgets/2026-08/RUB/restore" });
    assert.match(renderedText(harness.renderer.toJSON()), /v4/);
  } finally { await harness.cleanup(); }
});

test("a soft-deleted period is a restore state and never an empty create state", async () => {
  const deleted = budgetGroup({ deleted_at: "2026-08-27T10:00:00Z", version: 3 });
  const harness = await createHarness({ groups: [deleted] });
  try {
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /План RUB удалён/);
    assert.match(text, /Восстановить бюджет/);
    assert.doesNotMatch(text, /Создать бюджет|Изменить план|Удалить план бюджета|Скопировать прошлый месяц/);
  } finally { await harness.cleanup(); }
});

test("soft delete retries an uncertain network command with the same idempotency key", async () => {
  const keys: string[] = [];
  const deleted = budgetGroup({ deleted_at: "2026-08-27T10:00:00Z", version: 3 });
  const harness = await createHarness({ groups: [budgetGroup()], onRequest: async (_path, init) => {
    keys.push(new Headers(init.headers).get("X-Idempotency-Key") ?? "");
    if (keys.length === 1) throw new ApiClientError("Network unavailable", "API_NETWORK_ERROR", 0);
    return deleted;
  } });
  try {
    await act(async () => { button(harness.renderer, "Удалить план бюджета").props.onClick(); });
    await act(async () => { button(harness.renderer, "Удалить план бюджета", true).props.onClick(); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /Network unavailable/);
    assert.match(renderedText(harness.renderer.toJSON()), /Повторить ту же команду/);
    await act(async () => { button(harness.renderer, "Повторить ту же команду").props.onClick(); await settle(); });
    assert.equal(keys.length, 2);
    assert.ok(keys[0].length > 10);
    assert.equal(keys[0], keys[1]);
    assert.match(renderedText(harness.renderer.toJSON()), /soft delete/i);
  } finally { await harness.cleanup(); }
});

test("null usage percent stays unavailable instead of becoming a fake zero or infinity", async () => {
  const group = budgetGroup({
    allocations: [
      { ...budgetGroup().allocations[0], planned: "0.0000", usage_percent: null },
    ],
  });
  const harness = await createHarness({ groups: [group] });
  try {
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Процент недоступен/);
    assert.doesNotMatch(text, /Infinity|NaN/);
    assert.equal(harness.renderer.root.findAllByProps({ role: "progressbar" }).length, 0);
  } finally { await harness.cleanup(); }
});

test("restore category conflict is explicit and never removes offending allocations", async () => {
  const deleted = budgetGroup({ deleted_at: "2026-08-27T10:00:00Z", version: 3 });
  const harness = await createHarness({ groups: [deleted], onRequest: async () => {
    throw new ApiClientError("Category unavailable", "BUDGET_RESTORE_CATEGORY_CONFLICT", 409, undefined, { category_ids: ["category-child"] });
  } });
  try {
    await act(async () => { button(harness.renderer, "Восстановить бюджет").props.onClick(); await settle(); });
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /часть категорий плана больше недоступна/);
    assert.match(text, /Затронуто категорий: 1/);
    assert.match(text, /category-child/);
    assert.match(text, /Восстановить бюджет/);
  } finally { await harness.cleanup(); }
});

test("frozen historical plan and viewer hide every mutation while history stays available", async () => {
  const frozen = budgetGroup({ frozen: true, projection_source: "month_close_revision" });
  const frozenHarness = await createHarness({ groups: [frozen] });
  try {
    const text = renderedText(frozenHarness.renderer.toJSON());
    assert.match(text, /Закрытый месяц/);
    assert.match(text, /План зафиксирован закрытием месяца/);
    assert.match(text, /История изменений/);
    assert.doesNotMatch(text, /Изменить план|Удалить план бюджета|Скопировать прошлый месяц с заменой/);
  } finally { await frozenHarness.cleanup(); }

  const deletedFrozen = await createHarness({ groups: [budgetGroup({ deleted_at: "2026-08-27T10:00:00Z", frozen: true, projection_source: "month_close_revision" })] });
  try {
    const text = renderedText(deletedFrozen.renderer.toJSON());
    assert.match(text, /Восстановление недоступно/);
    assert.doesNotMatch(text, /Восстановить бюджет/);
  } finally { await deletedFrozen.cleanup(); }

  const frozenEmpty = await createHarness({ month: () => ({
    groups: [],
    historical_snapshot_available: true,
    period: "2026-08",
    projection_source: "month_close_revision",
    timezone: "Asia/Yekaterinburg",
  }) });
  try {
    const text = renderedText(frozenEmpty.renderer.toJSON());
    assert.match(text, /отсутствует в закрытом снимке/);
    assert.match(text, /только чтение/);
    assert.doesNotMatch(text, /Создать бюджет|Скопировать прошлый месяц|Валюта нового бюджета/);
  } finally { await frozenEmpty.cleanup(); }

  const viewer = await createHarness({ groups: [budgetGroup()], role: "viewer" });
  try {
    const text = renderedText(viewer.renderer.toJSON());
    assert.match(text, /Viewer · только чтение/);
    assert.doesNotMatch(text, /Изменить план|Удалить план бюджета|Скопировать прошлый месяц с заменой/);
  } finally { await viewer.cleanup(); }
});

test("version conflict reloads latest aggregate and does not blindly retry", async () => {
  let mutationCalls = 0;
  const harness = await createHarness({
    month: (read) => budgetMonth([budgetGroup({ version: read === 1 ? 2 : 11 })]),
    onRequest: async () => {
      mutationCalls += 1;
      throw new ApiClientError("Stale version", "BUDGET_VERSION_CONFLICT", 409);
    },
  });
  try {
    await act(async () => { button(harness.renderer, "Изменить план").props.onClick(); });
    const drawer = harness.renderer.root.findByProps({ "aria-label": "Редактирование бюджета" });
    await act(async () => { drawer.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(mutationCalls, 1);
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /старая команда не повторялась/);
    assert.match(text, /v11/);
  } finally { await harness.cleanup(); }
});

test("history is paginated and backend validation stays human-readable", async () => {
  const historyPaths: string[] = [];
  const revisions = Array.from({ length: 21 }, (_, index) => ({
    action: index ? "update" as const : "create" as const,
    actor_user_id: `user-${index}`,
    budget_period_id: "budget-rub",
    created_at: `2026-08-${String(Math.min(index + 1, 27)).padStart(2, "0")}T10:00:00Z`,
    id: `revision-${index}`,
    request_id: null,
    revision_number: index + 1,
    snapshot: {},
  }));
  const harness = await createHarness({ groups: [budgetGroup()], history: async (path) => {
    historyPaths.push(path);
    const offset = Number(new URLSearchParams(path.split("?")[1]).get("offset"));
    return { items: offset ? [revisions[19], revisions[20]] : revisions.slice(0, 20), page: { limit: 20, offset, total: revisions.length } };
  } });
  try {
    await act(async () => { button(harness.renderer, "История изменений").props.onClick(); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /#1/);
    await act(async () => { button(harness.renderer, "Загрузить ещё").props.onClick(); await settle(); });
    assert.deepEqual(historyPaths, [
      "/api/v1/budgets/2026-08/RUB/history?limit=20&offset=0",
      "/api/v1/budgets/2026-08/RUB/history?limit=20&offset=20",
    ]);
    assert.match(renderedText(harness.renderer.toJSON()), /#21/);
    assert.equal(renderedText(harness.renderer.toJSON()).match(/#20/g)?.length, 1);
  } finally { await harness.cleanup(); }

  const invalid = await createHarness({ onRequest: async () => {
    throw new ApiClientError("Allocation total is invalid", "BUDGET_ALLOCATION_INVALID", 422);
  } });
  try {
    await act(async () => { button(invalid.renderer, "Создать бюджет").props.onClick(); });
    const drawer = invalid.renderer.root.findByProps({ "aria-label": "Создание бюджета" });
    const inputs = drawer.findAllByType("input");
    await act(async () => { inputs[1].props.onChange({ target: { value: "1000" } }); });
    await act(async () => { drawer.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    const text = renderedText(invalid.renderer.toJSON());
    assert.match(text, /Проверьте суммы и состав распределений/);
    assert.match(text, /Allocation total is invalid/);
    assert.doesNotMatch(text, /\{"error"/);
  } finally { await invalid.cleanup(); }
});

test("history resets between currency groups and never leaks old revisions", async () => {
  const usd = budgetGroup({ currency: "USD", id: "budget-usd", version: 4 });
  const harness = await createHarness({ groups: [budgetGroup(), usd], history: async (path) => ({
    items: [{
      action: "update",
      actor_user_id: "user-1",
      budget_period_id: path.includes("/USD/") ? "budget-usd" : "budget-rub",
      created_at: "2026-08-27T10:00:00Z",
      id: path.includes("/USD/") ? "usd-history" : "rub-history",
      request_id: null,
      revision_number: path.includes("/USD/") ? 22 : 11,
      snapshot: {},
    }],
    page: { limit: 20, offset: 0, total: 1 },
  }) });
  try {
    await act(async () => { button(harness.renderer, "История изменений").props.onClick(); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /#11/);
    const historyDrawer = harness.renderer.root.findByProps({ "aria-label": "История изменений бюджета" });
    await act(async () => { historyDrawer.findByProps({ "aria-label": "Закрыть" }).props.onClick(); });
    const usdTab = harness.renderer.root.findAllByProps({ role: "tab" }).find((node) => node.findByType("strong").children.includes("USD"));
    assert.ok(usdTab);
    await act(async () => { usdTab.props.onClick(); });
    await act(async () => { button(harness.renderer, "История изменений").props.onClick(); await settle(); });
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /#22/);
    assert.doesNotMatch(text, /#11/);
  } finally { await harness.cleanup(); }
});
