import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { ApiClientError, apiClient } from "@/lib/api-client";
import type { BudgetGroup, BudgetMonth, BudgetRevisionPage } from "@/types/budget";
import type { BudgetForecastResponse } from "@/types/budget-forecast";
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

function budgetForecast(overrides: Partial<BudgetForecastResponse> = {}): BudgetForecastResponse {
  return {
    actual: { adjustment: "5.0000", expense: "125.0000", income: "1000.0000", net_cashflow: "875.0000" },
    advisory: { expense: "0.0000", income: "0.0000", occurrence_count: 0 },
    as_of: "2026-08-20T09:00:00Z",
    budget_id: "budget-rub",
    budget_version: 2,
    category_forecast: [],
    currency: "RUB",
    exceptions: { blocked_rule_count: 0, count: 0, failed_count: 0, incomplete_count: 0, materialized_excluded_count: 0, overdue_count: 0, skipped_count: 0 },
    forecast: {
      expense: "0.0000",
      income: "0.0000",
      mode_breakdown: [],
      net_cashflow: "0.0000",
      occurrence_count: 0,
      pending_draft_expense: "0.0000",
      pending_draft_income: "0.0000",
      pending_draft_occurrence_count: 0,
      scheduled_expense: "0.0000",
      scheduled_income: "0.0000",
      scheduled_occurrence_count: 0,
    },
    forecast_basis: "current_recurring_rules",
    generated_at: "2026-08-20T09:00:01Z",
    informational_transfers: { occurrence_count: 0, volume: "0.0000" },
    materialized_actual_occurrence_count: 0,
    occurrences: [],
    period: "2026-08",
    period_state: "open_current",
    projected: { adjustment: "5.0000", expense: "125.0000", income: "1000.0000", net_cashflow: "875.0000" },
    projection_source: "live",
    timezone: "Asia/Yekaterinburg",
    unbudgeted_forecast_expense: "0.0000",
    ...overrides,
  };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await new Promise<void>((resolve) => setImmediate(resolve));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
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
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    removeEventListener: () => undefined,
    requestAnimationFrame: (callback: () => void) => { callback(); return 1; },
    setTimeout: globalThis.setTimeout.bind(globalThis),
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
  forecast?: (read: number, path: string, init: RequestInit) => BudgetForecastResponse | Promise<BudgetForecastResponse>;
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
  const forecastPaths: string[] = [];
  const forecastSignals: Array<AbortSignal | null> = [];
  let budgetRead = 0;
  let forecastRead = 0;
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
    if (init.method === "GET" && path.includes("/forecast")) {
      forecastPaths.push(path);
      forecastSignals.push(init.signal ?? null);
      forecastRead += 1;
      return Promise.resolve(options.forecast?.(forecastRead, path, init) ?? budgetForecast()) as Promise<T>;
    }
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
    forecastPaths,
    forecastSignals,
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
    assert.deepEqual(owner.forecastPaths, []);
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
    assert.deepEqual(harness.forecastPaths, ["/api/v1/budgets/2026-08/RUB/forecast"]);
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
    assert.ok(harness.forecastPaths.length >= 2, "successful Budget mutation refreshes forecast");
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
    assert.match(text, /Регулярных операций до конца периода нет/);
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
    assert.doesNotMatch(renderedText(harness.renderer.toJSON()), /Прогноз до конца месяца/);
    assert.equal(harness.forecastPaths.length, 1, "soft delete clears forecast without a deleted-budget request");
    await act(async () => { button(harness.renderer, "Восстановить бюджет").props.onClick(); await settle(); });
    assert.deepEqual(requests[1], { body: { version: 3 }, method: "POST", path: "/api/v1/budgets/2026-08/RUB/restore" });
    assert.match(renderedText(harness.renderer.toJSON()), /v4/);
    assert.equal(harness.forecastPaths.length, 2, "restore refetches forecast");
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

test("forecast renders independent backend totals, modes, advisory, transfers, exceptions and exact category projection", async () => {
  const response = budgetForecast({
    actual: { adjustment: "104.0000", expense: "102.0000", income: "101.0000", net_cashflow: "103.0000" },
    advisory: { expense: "402.0000", income: "401.0000", occurrence_count: 2 },
    category_forecast: [{
      actual_expense: "501.0000",
      allocated_amount: "500.0000",
      category_id: "category-parent",
      category_name: "Имя из forecast намеренно отличается",
      forecast_expense: "502.0000",
      projected_expense: "503.0000",
      projected_remaining: "-33.0000",
      projected_usage_percent: "133.0000",
    }],
    exceptions: { blocked_rule_count: 1, count: 6, failed_count: 1, incomplete_count: 1, materialized_excluded_count: 1, overdue_count: 1, skipped_count: 1 },
    forecast: {
      expense: "202.0000",
      income: "201.0000",
      mode_breakdown: [
        { expense: "12.0000", income: "11.0000", mode: "confirmed", occurrence_count: 1 },
        { expense: "22.0000", income: "21.0000", mode: "draft", occurrence_count: 2 },
      ],
      net_cashflow: "203.0000",
      occurrence_count: 3,
      pending_draft_expense: "22.0000",
      pending_draft_income: "21.0000",
      pending_draft_occurrence_count: 2,
      scheduled_expense: "12.0000",
      scheduled_income: "11.0000",
      scheduled_occurrence_count: 1,
    },
    informational_transfers: { occurrence_count: 1, volume: "601.0000" },
    projected: { adjustment: "904.0000", expense: "902.0000", income: "901.0000", net_cashflow: "903.0000" },
    unbudgeted_forecast_expense: "77.0000",
  });
  const harness = await createHarness({ groups: [budgetGroup()], forecast: () => response });
  try {
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Прогноз до конца месяца/);
    assert.match(text, /По состоянию на/);
    for (const amount of ["101,00", "102,00", "103,00", "201,00", "202,00", "203,00", "901,00", "902,00", "903,00"]) assert.match(text, new RegExp(amount.replace(",", "[,\\s]")));
    assert.doesNotMatch(text, /302,00/);
    assert.match(text, /Подтверждённые · 1/);
    assert.match(text, /Черновики · 2/);
    assert.match(text, /Черновики могут требовать подтверждения/);
    assert.match(text, /Напоминания · 2/);
    assert.match(text, /Переводы · 1/);
    assert.match(text, /Прогноз содержит исключения: 6/);
    assert.match(text, /1 просрочено/);
    assert.match(text, /1 заблокировано/);
    assert.match(text, /Прогноз вне бюджета/);
    assert.match(text, /Дом/);
    assert.doesNotMatch(text, /Имя из forecast намеренно отличается/);
    assert.match(text, /ожидается перерасход/);
    const projectedRemaining = harness.renderer.root.findAllByProps({ "data-label": "Ожидаемый остаток" }).find((node) => renderedText(node).includes("33,00"));
    assert.ok(projectedRemaining);
    const projectedProgress = harness.renderer.root.findAllByProps({ role: "progressbar" }).find((node) => String(node.props["aria-valuetext"]).includes("133.0000%"));
    assert.ok(projectedProgress);
    assert.equal(projectedProgress.props["aria-valuenow"], 100);
    assert.equal(harness.forecastPaths[0], "/api/v1/budgets/2026-08/RUB/forecast");
  } finally { await harness.cleanup(); }
});

test("forecast period states are presented from backend semantics", async () => {
  const cases = [
    ["open_current", null],
    ["open_future", "Прогноз для выбранного будущего месяца"],
    ["open_past", "Период завершён — оставшийся прогноз равен нулю"],
    ["closed", "Исторический план зафиксирован при закрытии месяца"],
  ] as const;
  for (const [periodState, expected] of cases) {
    const harness = await createHarness({ groups: [budgetGroup()], forecast: () => budgetForecast({ period_state: periodState }) });
    try {
      const text = renderedText(harness.renderer.toJSON());
      if (expected) assert.match(text, new RegExp(expected));
      else assert.doesNotMatch(text, /Период завершён|будущего месяца|Исторический план/);
    } finally { await harness.cleanup(); }
  }
});

test("forecast error semantics stay local while auth errors reach the global handler", async () => {
  const cases = [
    [new ApiClientError("budget missing", "BUDGET_NOT_FOUND", 404), null],
    [new ApiClientError("route missing", "API_ERROR", 404), "Прогноз регулярных операций пока не поддерживается"],
    [new ApiClientError("method missing", "API_ERROR", 405), "Прогноз регулярных операций пока не поддерживается"],
    [new ApiClientError("cap", "BUDGET_FORECAST_LIMIT_EXCEEDED", 422), "Слишком много регулярных операций"],
    [new ApiClientError("failure", "FORECAST_FAILED", 500), "Backend временно не смог рассчитать прогноз"],
  ] as const;
  for (const [error, expected] of cases) {
    const harness = await createHarness({ groups: [budgetGroup()], forecast: async () => { throw error; } });
    try {
      const text = renderedText(harness.renderer.toJSON());
      assert.match(text, /План по категориям/);
      if (expected) assert.match(text, new RegExp(expected));
      else {
        assert.doesNotMatch(text, /Прогноз не загрузился/);
        assert.doesNotMatch(text, /пока не поддерживается/);
      }
      assert.equal(harness.errors.length, 0);
    } finally { await harness.cleanup(); }
  }

  for (const status of [401, 403]) {
    const auth = await createHarness({ groups: [budgetGroup()], forecast: async () => { throw new ApiClientError("expired", "AUTH_REQUIRED", status); } });
    try {
      assert.equal(auth.errors.length, 1);
      assert.match(renderedText(auth.renderer.toJSON()), /Бюджет остаётся доступен/);
      assert.doesNotMatch(renderedText(auth.renderer.toJSON()), /пока не поддерживается/);
    } finally { await auth.cleanup(); }
  }
});

test("occurrence drawer is lazy, read-only and preserves moved draft and backend amount source", async () => {
  const occurrences: BudgetForecastResponse["occurrences"] = [
    { amount: "10.0000", amount_source: "rule", category_id: "category-parent", category_name: "Дом", currency: "RUB", effective_at: "2026-08-12T09:00:00Z", execution_id: "execution-1", execution_status: "scheduled", reason: null, rule_id: "rule-1", rule_mode: "confirmed", rule_name: "Аренда", rule_timezone: "Asia/Yekaterinburg", scheduled_for: "2026-08-12T09:00:00Z", scheduled_for_workspace_local: "2026-08-12T14:00:00+05:00", state: "scheduled", transaction_id: null, transaction_status: null, transaction_type: "expense" },
    { amount: "987.6543", amount_source: "linked_transaction", category_id: "category-child", category_name: "Коммунальные услуги", currency: "RUB", effective_at: "2026-08-18T09:00:00Z", execution_id: "execution-2", execution_status: "pending", reason: "Перенесено вручную", rule_id: "rule-2", rule_mode: "draft", rule_name: "Перенесённый черновик", rule_timezone: "Europe/Moscow", scheduled_for: "2026-08-15T09:00:00Z", scheduled_for_workspace_local: "2026-08-15T14:00:00+05:00", state: "pending_draft", transaction_id: "transaction-2", transaction_status: "draft", transaction_type: "expense" },
    { amount: "30.0000", amount_source: "rule", category_id: null, category_name: null, currency: "RUB", effective_at: "2026-08-20T09:00:00Z", execution_id: null, execution_status: null, reason: "Требует решения", rule_id: "rule-3", rule_mode: "confirmed", rule_name: "Напоминание", rule_timezone: "UTC", scheduled_for: "2026-08-20T09:00:00Z", scheduled_for_workspace_local: "2026-08-20T14:00:00+05:00", state: "advisory", transaction_id: null, transaction_status: null, transaction_type: "income" },
    { amount: "40.0000", amount_source: "rule", category_id: null, category_name: null, currency: "RUB", effective_at: "2026-08-21T09:00:00Z", execution_id: null, execution_status: null, reason: null, rule_id: "rule-4", rule_mode: "confirmed", rule_name: "Перевод", rule_timezone: "UTC", scheduled_for: "2026-08-21T09:00:00Z", scheduled_for_workspace_local: "2026-08-21T14:00:00+05:00", state: "informational_transfer", transaction_id: null, transaction_status: null, transaction_type: "transfer" },
    { amount: "50.0000", amount_source: "rule", category_id: null, category_name: null, currency: "RUB", effective_at: "2026-08-22T09:00:00Z", execution_id: "execution-5", execution_status: "failed", reason: "Счёт недоступен", rule_id: "rule-5", rule_mode: "confirmed", rule_name: "Ошибка правила", rule_timezone: "UTC", scheduled_for: "2026-08-22T09:00:00Z", scheduled_for_workspace_local: "2026-08-22T14:00:00+05:00", state: "exception", transaction_id: null, transaction_status: null, transaction_type: "expense" },
  ];
  const summary = budgetForecast({ advisory: { expense: "30.0000", income: "0.0000", occurrence_count: 1 }, exceptions: { blocked_rule_count: 0, count: 1, failed_count: 1, incomplete_count: 0, materialized_excluded_count: 0, overdue_count: 0, skipped_count: 0 }, forecast: { ...budgetForecast().forecast, occurrence_count: 2, scheduled_occurrence_count: 1, pending_draft_occurrence_count: 1 }, informational_transfers: { occurrence_count: 1, volume: "40.0000" }, projected: { adjustment: "0.0000", expense: "0.0000", income: "777.0000", net_cashflow: "777.0000" } });
  const details = { ...summary, as_of: "2026-08-20T09:05:00Z", occurrences, projected: { adjustment: "0.0000", expense: "0.0000", income: "9999.0000", net_cashflow: "9999.0000" } };
  const harness = await createHarness({ groups: [budgetGroup()], forecast: (_read, path) => path.includes("include_occurrences=true") ? details : summary });
  try {
    assert.deepEqual(harness.forecastPaths, ["/api/v1/budgets/2026-08/RUB/forecast"]);
    assert.match(renderedText(harness.renderer.toJSON()), /777,00/);
    await act(async () => { button(harness.renderer, "Показать операции прогноза").props.onClick(); await settle(); });
    assert.equal(harness.forecastPaths[1], "/api/v1/budgets/2026-08/RUB/forecast?include_occurrences=true");
    const drawer = harness.renderer.root.findByProps({ "aria-label": "Вхождения прогноза бюджета" });
    const text = renderedText(drawer);
    assert.match(text, /Аренда/);
    assert.match(text, /Перенесённый черновик/);
    assert.match(text, /Напоминание/);
    assert.match(text, /Перевод/);
    assert.match(text, /Ошибка правила/);
    assert.match(text, /Дата в бюджете/);
    assert.match(text, /Запланировано/);
    assert.match(text, /Связанная операция/);
    assert.match(text, /987[,\s]6543/);
    assert.match(text, /Europe\/Moscow/);
    assert.match(text, /Перенесено вручную/);
    assert.doesNotMatch(text, /Редактировать|Выполнить|Запустить сейчас|Исправить/);
    assert.match(renderedText(harness.renderer.toJSON()), /777,00/);
    assert.doesNotMatch(renderedText(harness.renderer.toJSON()), /9[\s ]?999,00/);
    const recurringLink = drawer.findAllByType("a").find((node) => node.props.href === "/recurring");
    assert.ok(recurringLink);
    await act(async () => { drawer.findByProps({ "aria-label": "Закрыть" }).props.onClick(); await settle(); });
    await act(async () => { button(harness.renderer, "Показать операции прогноза").props.onClick(); await settle(); });
    assert.equal(harness.forecastPaths.filter((path) => path.includes("include_occurrences=true")).length, 2, "each explicit drawer open refetches one detail snapshot");
  } finally { await harness.cleanup(); }
});

test("late forecast response cannot overwrite a new currency and requests abort on switch and unmount", async () => {
  const rub = deferred<BudgetForecastResponse>();
  const usd = deferred<BudgetForecastResponse>();
  const usdGroup = budgetGroup({ currency: "USD", id: "budget-usd", version: 3 });
  const harness = await createHarness({ groups: [budgetGroup(), usdGroup], forecast: (_read, path) => path.includes("/USD/") ? usd.promise : rub.promise });
  try {
    const usdTab = harness.renderer.root.findAllByProps({ role: "tab" }).find((node) => node.findByType("strong").children.includes("USD"));
    assert.ok(usdTab);
    await act(async () => { usdTab.props.onClick(); await settle(); });
    assert.equal(harness.forecastSignals[0]?.aborted, true);
    await act(async () => { usd.resolve(budgetForecast({ currency: "USD", projected: { adjustment: "0.0000", expense: "0.0000", income: "2222.0000", net_cashflow: "2222.0000" } })); await settle(); });
    await act(async () => { rub.resolve(budgetForecast({ projected: { adjustment: "0.0000", expense: "0.0000", income: "1111.0000", net_cashflow: "1111.0000" } })); await settle(); });
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /2[\s ]?222,00/);
    assert.doesNotMatch(text, /1[\s ]?111,00/);
  } finally {
    const currentSignal = harness.forecastSignals.at(-1);
    await harness.cleanup();
    assert.equal(currentSignal?.aborted, true);
  }
});

test("late August summary cannot render after switching to September", async () => {
  const august = deferred<BudgetForecastResponse>();
  const september = deferred<BudgetForecastResponse>();
  const septemberGroup = budgetGroup({ id: "budget-september", period: "2026-09", version: 3 });
  const harness = await createHarness({
    groups: [budgetGroup()],
    month: (read) => read === 1
      ? budgetMonth([budgetGroup()])
      : { ...budgetMonth([septemberGroup]), period: "2026-09" },
    forecast: (_read, path) => path.includes("/2026-09/") ? september.promise : august.promise,
  });
  try {
    const monthInput = harness.renderer.root.findByProps({ "aria-label": "Период бюджета" });
    await act(async () => { monthInput.props.onChange({ target: { value: "2026-09" } }); await settle(); });
    assert.equal(harness.forecastSignals[0]?.aborted, true);
    assert.doesNotMatch(renderedText(harness.renderer.toJSON()), /1[\s ]?111,00/);
    await act(async () => { september.resolve(budgetForecast({ period: "2026-09", projected: { adjustment: "0.0000", expense: "0.0000", income: "2222.0000", net_cashflow: "2222.0000" } })); await settle(); });
    await act(async () => { august.resolve(budgetForecast({ projected: { adjustment: "0.0000", expense: "0.0000", income: "1111.0000", net_cashflow: "1111.0000" } })); await settle(); });
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /2[\s ]?222,00/);
    assert.doesNotMatch(text, /1[\s ]?111,00/);
  } finally { await harness.cleanup(); }
});

test("period switch aborts pending details and closes the drawer without fetching new details", async () => {
  const augustDetails = deferred<BudgetForecastResponse>();
  const summary = budgetForecast({ forecast: { ...budgetForecast().forecast, occurrence_count: 1, scheduled_occurrence_count: 1 } });
  const septemberGroup = budgetGroup({ id: "budget-september", period: "2026-09", version: 3 });
  const harness = await createHarness({
    groups: [budgetGroup()],
    month: (read) => read === 1
      ? budgetMonth([budgetGroup()])
      : { ...budgetMonth([septemberGroup]), period: "2026-09" },
    forecast: (_read, path) => path.includes("include_occurrences=true")
      ? augustDetails.promise
      : path.includes("/2026-09/") ? { ...summary, period: "2026-09" } : summary,
  });
  try {
    await act(async () => { button(harness.renderer, "Показать операции прогноза").props.onClick(); await settle(); });
    const detailSignal = harness.forecastSignals[1];
    assert.ok(harness.renderer.root.findByProps({ "aria-label": "Вхождения прогноза бюджета" }));
    const monthInput = harness.renderer.root.findByProps({ "aria-label": "Период бюджета" });
    await act(async () => { monthInput.props.onChange({ target: { value: "2026-09" } }); await settle(); });
    assert.equal(detailSignal?.aborted, true);
    assert.equal(harness.renderer.root.findAllByProps({ "aria-label": "Вхождения прогноза бюджета" }).length, 0);
    assert.equal(harness.forecastPaths.filter((path) => path.includes("include_occurrences=true")).length, 1);
    await act(async () => { augustDetails.resolve({ ...summary, occurrences: [{ amount: "1.0000", amount_source: "rule", category_id: null, category_name: null, currency: "RUB", effective_at: "2026-08-20T09:00:00Z", execution_id: null, execution_status: null, reason: null, rule_id: "old-rule", rule_mode: "confirmed", rule_name: "Старое августовское вхождение", rule_timezone: "UTC", scheduled_for: "2026-08-20T09:00:00Z", scheduled_for_workspace_local: "2026-08-20T14:00:00+05:00", state: "scheduled", transaction_id: null, transaction_status: null, transaction_type: "expense" }] }); await settle(); });
    assert.doesNotMatch(renderedText(harness.renderer.toJSON()), /Старое августовское вхождение/);
  } finally { await harness.cleanup(); }
});

test("failed forecast refresh keeps prior data only with an explicit stale warning", async () => {
  const harness = await createHarness({ groups: [budgetGroup()], forecast: async (read) => {
    if (read === 1) return budgetForecast({ projected: { adjustment: "0.0000", expense: "0.0000", income: "777.0000", net_cashflow: "777.0000" } });
    throw new ApiClientError("offline", "API_NETWORK_ERROR", 0);
  } });
  try {
    assert.match(renderedText(harness.renderer.toJSON()), /777,00/);
    const refresh = harness.renderer.root.findByProps({ "aria-label": "Обновить прогноз" });
    await act(async () => { refresh.props.onClick(); await settle(); });
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /777,00/);
    assert.match(text, /Показан предыдущий успешный расчёт/);
    assert.match(text, /сетевой ошибки/);
  } finally { await harness.cleanup(); }
});
