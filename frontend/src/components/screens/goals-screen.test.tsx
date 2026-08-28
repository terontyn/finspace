import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { ApiClientError, apiClient } from "@/lib/api-client";
import type {
  Goal,
  GoalContribution,
  GoalContributionCommandResponse,
  GoalContributionPage,
  GoalPage,
} from "@/types/goals";

import { GoalsScreen } from "./goals-screen";

function goal(overrides: Partial<Goal> = {}): Goal {
  return {
    contributed_amount: "25.0000",
    contribution_count: 1,
    created_at: "2026-08-01T00:00:00Z",
    created_by: "user-1",
    currency: "RUB",
    days_remaining: 10,
    deleted_at: null,
    description: "Финансовый резерв",
    id: "goal-1",
    is_target_reached: false,
    name: "Подушка",
    overdue: false,
    progress_percent: "25.0000",
    remaining_amount: "75.0000",
    status: "active",
    target_amount: "100.0000",
    target_date: "2026-09-07",
    updated_at: "2026-08-02T00:00:00Z",
    updated_by: "user-1",
    version: 3,
    workspace_id: "workspace-1",
    ...overrides,
  };
}

function contribution(overrides: Partial<GoalContribution> = {}): GoalContribution {
  return {
    amount: "25.0000",
    contributed_at: "2026-08-02T10:00:00Z",
    correction_of_id: null,
    created_at: "2026-08-02T10:00:00Z",
    created_by: "user-1",
    created_by_display_name: "Никита",
    currency: "RUB",
    goal_id: "goal-1",
    id: "contribution-1",
    note: "Первый вклад",
    workspace_id: "workspace-1",
    ...overrides,
  };
}

function page(items: Goal[], offset = 0, total = items.length): GoalPage {
  return { items, page: { limit: 12, offset, total } };
}

function historyPage(items: GoalContribution[], offset = 0, total = items.length): GoalContributionPage {
  return { items, page: { limit: 20, offset, total } };
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
  if (value && typeof value === "object" && "children" in value) {
    return renderedText((value as { children?: unknown }).children);
  }
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
  for (const key of ["window", "self", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
    descriptors.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  }
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
  detail?: (id: string, read: number) => Goal | Promise<Goal>;
  history?: (id: string, path: string) => GoalContributionPage | Promise<GoalContributionPage>;
  list?: (read: number, path: string) => GoalPage | Promise<GoalPage>;
  onRequest?: (path: string, init: RequestInit) => Promise<unknown>;
  role?: string;
}

async function createHarness(options: HarnessOptions = {}) {
  const originalGet = apiClient.get;
  const originalRequest = apiClient.request;
  const restoreBrowser = installBrowserGlobals();
  const errors: unknown[] = [];
  const getPaths: string[] = [];
  let listRead = 0;
  let detailRead = 0;
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    getPaths.push(path);
    if (path === "/api/v1/auth/me") return Promise.resolve({ role: options.role ?? "owner" } as T);
    if (path.startsWith("/api/v1/goals?") || path === "/api/v1/goals") {
      listRead += 1;
      return Promise.resolve(options.list?.(listRead, path) ?? page([goal()])) as Promise<T>;
    }
    const match = path.match(/^\/api\/v1\/goals\/([^/?]+)/);
    if (!match) throw new Error(`Unexpected GET ${path}`);
    const goalId = decodeURIComponent(match[1]);
    if (path.includes("/contributions?")) {
      return Promise.resolve(options.history?.(goalId, path) ?? historyPage([contribution({ goal_id: goalId })])) as Promise<T>;
    }
    detailRead += 1;
    return Promise.resolve(options.detail?.(goalId, detailRead) ?? goal({ id: goalId })) as Promise<T>;
  }) as typeof apiClient.get;
  apiClient.request = (<T,>(path: string, init: RequestInit = {}) => {
    if (!options.onRequest) throw new Error(`Unexpected mutation ${init.method} ${path}`);
    return options.onRequest(path, init) as Promise<T>;
  }) as typeof apiClient.request;
  await act(async () => {
    renderer = create(<GoalsScreen onError={(error) => errors.push(error)} preferredCurrency="USD"/>);
    await settle();
  });
  if (!renderer) throw new Error("Goals renderer was not created");
  return {
    errors,
    getPaths,
    renderer,
    async cleanup() {
      await act(async () => renderer?.unmount());
      apiClient.get = originalGet;
      apiClient.request = originalRequest;
      restoreBrowser();
    },
  };
}

async function openFirstGoal(renderer: ReactTestRenderer) {
  await act(async () => { button(renderer, "Открыть").props.onClick(); await settle(); });
}

test("loading, API error and honest empty states cover owner and viewer", async () => {
  let resolveList: ((value: GoalPage) => void) | undefined;
  const pending = new Promise<GoalPage>((resolve) => { resolveList = resolve; });
  const loading = await createHarness({ list: () => pending });
  try {
    assert.ok(loading.renderer.root.findByProps({ "aria-label": "Загружаем цели" }));
    await act(async () => { resolveList?.(page([])); await settle(); });
    assert.match(renderedText(loading.renderer.toJSON()), /Создать цель/);
  } finally { await loading.cleanup(); }

  const viewer = await createHarness({ list: () => page([]), role: "viewer" });
  try {
    const text = renderedText(viewer.renderer.toJSON());
    assert.match(text, /Роль viewer/);
    assert.doesNotMatch(text, /＋ Создать цель/);
  } finally { await viewer.cleanup(); }

  const failed = await createHarness({ list: () => Promise.reject(new ApiClientError("offline", "API_NETWORK_ERROR", 0)) });
  try {
    assert.match(renderedText(failed.renderer.toJSON()), /Цели не загрузились/);
    assert.equal(failed.errors.length, 1);
  } finally { await failed.cleanup(); }
});

test("list renders independent currencies and clamps only the visual progress above 100", async () => {
  const rub = goal({ contributed_amount: "125.0000", is_target_reached: true, progress_percent: "125.0000", remaining_amount: "-25.0000" });
  const usd = goal({ currency: "USD", id: "goal-usd", name: "Travel", target_amount: "500.0000" });
  const harness = await createHarness({ list: () => page([rub, usd]) });
  try {
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /125%/);
    assert.match(text, /RUB/);
    assert.match(text, /USD/);
    assert.doesNotMatch(text, /Общий итог|Total saved|Всего накоплено/);
    const over = harness.renderer.root.findAllByProps({ role: "progressbar" })[0];
    assert.equal(over.props["aria-valuenow"], 100);
    assert.equal(over.props["aria-valuetext"], "125.0000%");
    assert.equal(over.findByType("i").props.style.width, "100%");
    assert.match(text, /Цель достигнута · lifecycle: Активна/);
  } finally { await harness.cleanup(); }
});

test("search, status, currency, deleted and pagination use backend query parameters", async () => {
  const items = Array.from({ length: 12 }, (_, index) => goal({ id: `goal-${index}`, name: `Цель ${index}` }));
  const harness = await createHarness({ list: (_read, path) => page(items, path.includes("offset=12") ? 12 : 0, 24) });
  try {
    assert.doesNotMatch(harness.getPaths.find((path) => path.startsWith("/api/v1/goals?")) ?? "", /include_deleted/);
    const toolbar = harness.renderer.root.findByProps({ "aria-label": "Фильтры целей" });
    const inputs = toolbar.findAllByType("input");
    const select = toolbar.findByType("select");
    await act(async () => { inputs[0].props.onChange({ target: { value: "резерв" } }); await settle(); });
    await act(async () => { select.props.onChange({ target: { value: "paused" } }); await settle(); });
    await act(async () => { inputs[1].props.onChange({ target: { value: "usd" } }); await settle(); });
    await act(async () => { inputs[2].props.onChange({ target: { checked: true } }); await settle(); });
    const finalFilterPath = harness.getPaths.filter((path) => path.startsWith("/api/v1/goals?")).at(-1) ?? "";
    assert.match(finalFilterPath, /status=paused/);
    assert.match(finalFilterPath, /currency=USD/);
    assert.match(finalFilterPath, /include_deleted=true/);
    assert.match(finalFilterPath, /search=%D1%80%D0%B5%D0%B7%D0%B5%D1%80%D0%B2/);
    await act(async () => { button(harness.renderer, "Далее →").props.onClick(); await settle(); });
    assert.match(harness.getPaths.filter((path) => path.startsWith("/api/v1/goals?")).at(-1) ?? "", /offset=12/);
  } finally { await harness.cleanup(); }
});

test("a stale list response cannot overwrite a newer filtered result", async () => {
  let resolveOld: ((value: GoalPage) => void) | undefined;
  const old = new Promise<GoalPage>((resolve) => { resolveOld = resolve; });
  const harness = await createHarness({
    list: (read) => read === 1 ? page([goal()]) : read === 2 ? old : page([goal({ id: "new", name: "Новая выборка" })]),
  });
  try {
    const search = harness.renderer.root.findByProps({ placeholder: "Название или описание" });
    await act(async () => { search.props.onChange({ target: { value: "старый" } }); await Promise.resolve(); });
    await act(async () => { search.props.onChange({ target: { value: "новый" } }); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /Новая выборка/);
    await act(async () => { resolveOld?.(page([goal({ id: "stale", name: "Устаревшая выборка" })])); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /Новая выборка/);
    assert.doesNotMatch(renderedText(harness.renderer.toJSON()), /Устаревшая выборка/);
  } finally { await harness.cleanup(); }
});

test("an older pending list cannot revert a newer detail projection", async () => {
  const oldProjection = goal({ name: "Старая карточка", progress_percent: "25.0000", version: 3 });
  const newProjection = goal({ name: "Новая карточка", progress_percent: "60.0000", version: 5 });
  let resolveRefresh: ((value: GoalPage) => void) | undefined;
  const pendingRefresh = new Promise<GoalPage>((resolve) => { resolveRefresh = resolve; });
  const harness = await createHarness({
    detail: () => newProjection,
    list: (read) => read === 1 ? page([oldProjection]) : pendingRefresh,
  });
  try {
    await openFirstGoal(harness.renderer);
    assert.match(renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" })), /Новая карточка.*60%/);
    await act(async () => { button(harness.renderer, "Обновить").props.onClick(); await Promise.resolve(); });

    await act(async () => { resolveRefresh?.(page([oldProjection])); await settle(); });
    const list = harness.renderer.root.findByProps({ "aria-label": "Список целей" });
    assert.match(renderedText(list), /Новая карточка.*60%/);
    assert.doesNotMatch(renderedText(list), /Старая карточка/);
  } finally { await harness.cleanup(); }
});

test("create uses workspace currency, stable retry key, semantic edit key and backend fields", async () => {
  const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
  let created = false;
  const result = goal({ currency: "USD", id: "created", name: "Отпуск", version: 1 });
  const harness = await createHarness({
    list: () => page(created ? [result] : []),
    onRequest: async (_path, init) => {
      requests.push({ body: JSON.parse(String(init.body)), key: new Headers(init.headers).get("X-Idempotency-Key") ?? "" });
      if (requests.length === 1) throw new ApiClientError("Network", "API_NETWORK_ERROR", 0);
      if (requests.length === 2) throw new ApiClientError("Invalid", "GOAL_CONTRIBUTION_INVALID", 422);
      created = true;
      return result;
    },
  });
  try {
    await act(async () => { button(harness.renderer, "Создать цель").props.onClick(); });
    const drawer = harness.renderer.root.findByProps({ "aria-label": "Создание цели" });
    const inputs = drawer.findAllByType("input");
    assert.equal(inputs[1].props.value, "USD");
    await act(async () => { inputs[0].props.onChange({ target: { value: "Отпуск" } }); });
    await act(async () => { drawer.findAllByType("input")[2].props.onChange({ target: { value: "1000" } }); });
    await act(async () => { drawer.findAllByType("input")[3].props.onChange({ target: { value: "2027-06-01" } }); });
    await act(async () => { drawer.findByType("textarea").props.onChange({ target: { value: "Море" } }); });
    await act(async () => { harness.renderer.root.findByProps({ "aria-label": "Создание цели" }).findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    await act(async () => { harness.renderer.root.findByProps({ "aria-label": "Создание цели" }).findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(requests[0].key, requests[1].key);
    await act(async () => { harness.renderer.root.findByProps({ "aria-label": "Создание цели" }).findAllByType("input")[2].props.onChange({ target: { value: "1100" } }); });
    await act(async () => { harness.renderer.root.findByProps({ "aria-label": "Создание цели" }).findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.notEqual(requests[1].key, requests[2].key);
    assert.deepEqual(requests[2].body, {
      currency: "USD",
      description: "Море",
      name: "Отпуск",
      target_amount: "1100",
      target_date: "2027-06-01",
    });
    assert.equal("status" in requests[2].body, false);
    assert.match(renderedText(harness.renderer.toJSON()), /Цель создана/);
  } finally { await harness.cleanup(); }
});

test("edit sends current version, never status, locks currency and resets identity after semantic edit", async () => {
  const captured: Array<{ body: Record<string, unknown>; key: string }> = [];
  const current = goal({ contribution_count: 2, version: 7 });
  const harness = await createHarness({
    detail: () => current,
    list: () => page([current]),
    onRequest: async (_path, init) => {
      captured.push({ body: JSON.parse(String(init.body)), key: new Headers(init.headers).get("X-Idempotency-Key") ?? "" });
      if (captured.length === 1) throw new ApiClientError("offline", "API_NETWORK_ERROR", 0);
      return goal({ ...current, name: "Совсем новая подушка", version: 8 });
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Изменить").props.onClick(); });
    const drawer = harness.renderer.root.findByProps({ "aria-label": "Редактирование цели" });
    const inputs = drawer.findAllByType("input");
    assert.equal(inputs[1].props.disabled, true);
    await act(async () => { inputs[0].props.onChange({ target: { value: "Новая подушка" } }); });
    await act(async () => { drawer.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    const currentDrawer = harness.renderer.root.findByProps({ "aria-label": "Редактирование цели" });
    await act(async () => { currentDrawer.findAllByType("input")[0].props.onChange({ target: { value: "Совсем новая подушка" } }); });
    await act(async () => { currentDrawer.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(captured[1].body.version, 7);
    assert.equal("status" in captured[1].body, false);
    assert.notEqual(captured[0].key, captured[1].key);
  } finally { await harness.cleanup(); }
});

test("contribution lost-response retry keeps its key and exact replay converges through fresh GET", async () => {
  const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
  const snapshot50 = goal({ contributed_amount: "50.0000", contribution_count: 2, progress_percent: "50.0000", remaining_amount: "50.0000", version: 4 });
  const live60 = goal({ contributed_amount: "60.0000", contribution_count: 3, progress_percent: "60.0000", remaining_amount: "40.0000", version: 5 });
  let detailRead = 0;
  const harness = await createHarness({
    detail: () => { detailRead += 1; return detailRead === 1 ? goal() : live60; },
    onRequest: async (_path, init) => {
      requests.push({ body: JSON.parse(String(init.body)), key: new Headers(init.headers).get("X-Idempotency-Key") ?? "" });
      if (requests.length === 1) throw new ApiClientError("Network", "API_NETWORK_ERROR", 0);
      return { contribution: contribution({ amount: "25.0000", id: "new-event" }), goal: snapshot50 } satisfies GoalContributionCommandResponse;
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Добавить вклад").props.onClick(); });
    const dialog = harness.renderer.root.findByType("form");
    await act(async () => {
      dialog.findByType("input").props.onChange({ target: { value: "25" } });
      dialog.findByType("textarea").props.onChange({ target: { value: "Вклад" } });
    });
    await act(async () => { dialog.props.onSubmit({ preventDefault() {} }); await settle(); });
    await act(async () => { dialog.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(requests[0].key, requests[1].key);
    assert.deepEqual(requests[1].body, { amount: "25", note: "Вклад" });
    const drawer = harness.renderer.root.findByProps({ "aria-label": "Детали цели" });
    assert.match(renderedText(drawer), /60%/);
    assert.doesNotMatch(renderedText(drawer), /50%/);
    assert.ok(harness.getPaths.filter((path) => path.startsWith("/api/v1/goals/goal-1?")).length >= 2);
  } finally { await harness.cleanup(); }
});

test("corrections are only offered for originals and send signed adjustments without mutating history", async () => {
  const original = contribution();
  const correctionRow = contribution({ amount: "-5.0000", correction_of_id: original.id, id: "correction-1", note: "Исправление" });
  const requests: Array<{ body: Record<string, unknown>; path: string }> = [];
  const harness = await createHarness({
    history: () => historyPage([original, correctionRow]),
    onRequest: async (path, init) => {
      requests.push({ body: JSON.parse(String(init.body)), path });
      return { contribution: correctionRow, goal: goal({ contributed_amount: "20.0000", progress_percent: "20.0000" }) };
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    assert.equal(harness.renderer.root.findAllByType("button").filter((node) => renderedText(node.props.children) === "Исправить").length, 1);
    await act(async () => { button(harness.renderer, "Исправить").props.onClick(); });
    const form = harness.renderer.root.findByType("form");
    await act(async () => {
      form.findByType("input").props.onChange({ target: { value: "-5" } });
      form.findByType("textarea").props.onChange({ target: { value: "Ошибка ввода" } });
    });
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(requests[0].path, "/api/v1/goals/goal-1/contributions/contribution-1/correct");
    assert.deepEqual(requests[0].body, { adjustment_amount: "-5", note: "Ошибка ввода" });
    await act(async () => { button(harness.renderer, "Исправить").props.onClick(); });
    const positive = harness.renderer.root.findByType("form");
    await act(async () => { positive.findByType("input").props.onChange({ target: { value: "7.5" } }); });
    await act(async () => { positive.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.deepEqual(requests[1].body, { adjustment_amount: "7.5", note: null });
  } finally { await harness.cleanup(); }
});

test("correction eligibility follows immutable-row, lifecycle, deleted and viewer semantics", async () => {
  for (const status of ["active", "paused", "completed", "cancelled"] as const) {
    const current = goal({ status });
    const harness = await createHarness({ detail: () => current, list: () => page([current]) });
    try {
      await openFirstGoal(harness.renderer);
      assert.equal(
        harness.renderer.root.findAllByType("button").filter((node) => renderedText(node.props.children) === "Исправить").length,
        1,
        `original contribution must remain correctable while Goal is ${status}`,
      );
    } finally { await harness.cleanup(); }
  }

  for (const options of [
    { current: goal({ deleted_at: "2026-08-04T00:00:00Z" }), role: "owner" },
    { current: goal(), role: "viewer" },
  ]) {
    const harness = await createHarness({ detail: () => options.current, list: () => page([options.current]), role: options.role });
    try {
      await openFirstGoal(harness.renderer);
      assert.equal(
        harness.renderer.root.findAllByType("button").filter((node) => renderedText(node.props.children) === "Исправить").length,
        0,
      );
    } finally { await harness.cleanup(); }
  }
});

test("a terminal contribution rejection retires its idempotency key", async () => {
  const keys: string[] = [];
  const harness = await createHarness({
    onRequest: async (_path, init) => {
      keys.push(new Headers(init.headers).get("X-Idempotency-Key") ?? "");
      throw new ApiClientError("invalid", "GOAL_CONTRIBUTION_INVALID", 422);
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Добавить вклад").props.onClick(); });
    const form = harness.renderer.root.findByType("form");
    await act(async () => { form.findByType("input").props.onChange({ target: { value: "10" } }); });
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(keys.length, 2);
    assert.notEqual(keys[0], keys[1]);
  } finally { await harness.cleanup(); }
});

test("history pagination deduplicates rows while offset advances by raw backend rows", async () => {
  const first = contribution();
  const second = contribution({ id: "contribution-2", note: "Второй вклад" });
  const historyPaths: string[] = [];
  const harness = await createHarness({
    history: (_id, path) => {
      historyPaths.push(path);
      return path.includes("offset=0") ? historyPage([first], 0, 2) : historyPage([first, second], 1, 2);
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Загрузить ещё").props.onClick(); await settle(); });
    const rows = harness.renderer.root.findAll((node) => typeof node.props.className === "string" && node.props.className.startsWith("goals-history-row"));
    assert.equal(rows.length, 2);
    assert.match(renderedText(rows), /Первый вклад/);
    assert.match(renderedText(rows), /Второй вклад/);
    assert.match(historyPaths[1], /offset=1/);
    assert.equal(harness.renderer.root.findAllByType("button").filter((node) => renderedText(node.props.children) === "Загрузить ещё").length, 0);
  } finally { await harness.cleanup(); }
});

test("lifecycle controls distinguish target reached, completed below target, cancelled and viewer", async () => {
  const cases: Array<{ absent: string[]; goal: Goal; present: string[]; role?: string }> = [
    { absent: ["Завершить", "Возобновить"], goal: goal(), present: ["Добавить вклад", "Приостановить", "Отменить цель"] },
    { absent: ["Добавить вклад"], goal: goal({ status: "paused" }), present: ["Возобновить", "Отменить цель"] },
    { absent: ["Возобновить"], goal: goal({ is_target_reached: true }), present: ["Завершить", "Цель достигнута"] },
    { absent: ["Добавить вклад", "Приостановить"], goal: goal({ is_target_reached: false, status: "completed" }), present: ["Вернуть в активные", "после исправления прогресс ниже цели"] },
    { absent: ["Добавить вклад", "Возобновить", "Вернуть в активные"], goal: goal({ status: "cancelled" }), present: ["Отменена"] },
    { absent: ["Изменить", "Добавить вклад", "Удалить"], goal: goal(), present: ["Viewer · просмотр"], role: "viewer" },
  ];
  for (const item of cases) {
    const harness = await createHarness({ detail: () => item.goal, list: () => page([item.goal]), role: item.role });
    try {
      await openFirstGoal(harness.renderer);
      const text = renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" }));
      for (const expected of item.present) assert.match(text, new RegExp(expected));
      for (const forbidden of item.absent) assert.doesNotMatch(text, new RegExp(forbidden));
    } finally { await harness.cleanup(); }
  }
});

test("lifecycle commands use exact versions and isolated operation identities", async () => {
  const commands: Array<{ body: Record<string, unknown>; key: string; path: string }> = [];
  const current = goal({ version: 11 });
  const harness = await createHarness({
    detail: () => current,
    list: () => page([current]),
    onRequest: async (path, init) => {
      commands.push({
        body: JSON.parse(String(init.body)),
        key: new Headers(init.headers).get("X-Idempotency-Key") ?? "",
        path,
      });
      return goal({ status: path.endsWith("/cancel") ? "cancelled" : "paused", version: 12 });
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Приостановить").props.onClick(); await settle(); });
    assert.equal(commands[0].path, "/api/v1/goals/goal-1/pause");
    assert.deepEqual(commands[0].body, { version: 11 });
    assert.ok(commands[0].key.length > 10);
    await act(async () => { button(harness.renderer, "Отменить цель").props.onClick(); await settle(); });
    assert.equal(commands[1].path, "/api/v1/goals/goal-1/cancel");
    assert.notEqual(commands[0].key, commands[1].key);
  } finally { await harness.cleanup(); }
});

test("delete confirms soft semantics and restore trusts cancelled lifecycle returned by backend", async () => {
  const deleted = goal({ deleted_at: "2026-08-03T00:00:00Z", status: "cancelled", version: 5 });
  const requests: Array<{ key: string; method: string; path: string }> = [];
  let current = goal({ status: "cancelled", version: 4 });
  let deleteAttempts = 0;
  const harness = await createHarness({
    detail: () => current,
    list: () => page([current]),
    onRequest: async (path, init) => {
      requests.push({ key: new Headers(init.headers).get("X-Idempotency-Key") ?? "", method: init.method ?? "", path });
      if (init.method === "DELETE") {
        deleteAttempts += 1;
        if (deleteAttempts === 1) throw new ApiClientError("Network", "API_NETWORK_ERROR", 0);
        current = deleted;
      }
      else current = goal({ status: "cancelled", version: 6 });
      return current;
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Удалить").props.onClick(); });
    assert.match(renderedText(harness.renderer.toJSON()), /soft delete|Lifecycle, вклад/i);
    await act(async () => { button(harness.renderer, "Удалить цель").props.onClick(); await settle(); });
    assert.equal(requests[0].path, "/api/v1/goals/goal-1?version=4");
    assert.equal(requests[0].method, "DELETE");
    await act(async () => { button(harness.renderer, "Повторить команду").props.onClick(); await settle(); });
    assert.equal(requests[0].key, requests[1].key);
    await act(async () => { button(harness.renderer, "Восстановить · Отменена").props.onClick(); await settle(); });
    assert.equal(requests[2].path, "/api/v1/goals/goal-1/restore");
    assert.notEqual(requests[1].key, requests[2].key);
    assert.match(renderedText(harness.renderer.toJSON()), /восстановлена со статусом «Отменена»/);
    assert.doesNotMatch(renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" })), /Возобновить|Вернуть в активные/);
  } finally { await harness.cleanup(); }
});

test("restore also preserves a completed lifecycle instead of inventing active", async () => {
  let current = goal({ deleted_at: "2026-08-03T00:00:00Z", is_target_reached: true, status: "completed", version: 5 });
  const harness = await createHarness({
    detail: () => current,
    list: () => page([current]),
    onRequest: async () => {
      current = goal({ is_target_reached: true, status: "completed", version: 6 });
      return current;
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Восстановить · Завершена").props.onClick(); await settle(); });
    const text = renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" }));
    assert.match(text, /Завершена/);
    assert.match(text, /Вернуть в активные/);
    assert.doesNotMatch(text, /Добавить вклад/);
  } finally { await harness.cleanup(); }
});

test("version conflict closes stale editor, reloads the goal and never retries automatically", async () => {
  let mutationCount = 0;
  const harness = await createHarness({
    detail: (_id, read) => goal({ name: read > 1 ? "Свежая цель" : "Подушка", version: read > 1 ? 9 : 3 }),
    onRequest: async () => {
      mutationCount += 1;
      throw new ApiClientError("stale", "GOAL_VERSION_CONFLICT", 409);
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Изменить").props.onClick(); });
    const form = harness.renderer.root.findByProps({ "aria-label": "Редактирование цели" }).findByType("form");
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(mutationCount, 1);
    assert.equal(harness.renderer.root.findAllByProps({ "aria-label": "Редактирование цели" }).length, 0);
    assert.match(renderedText(harness.renderer.toJSON()), /старая команда не повторялась/);
    assert.match(renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" })), /Свежая цель/);
  } finally { await harness.cleanup(); }
});

test("lifecycle, delete and restore version conflicts close stale actions without blind retry", async () => {
  const lifecycle = await createHarness({
    detail: (_id, read) => goal({ version: read > 1 ? 9 : 3 }),
    onRequest: async () => { throw new ApiClientError("stale", "GOAL_VERSION_CONFLICT", 409); },
  });
  try {
    await openFirstGoal(lifecycle.renderer);
    await act(async () => { button(lifecycle.renderer, "Приостановить").props.onClick(); await settle(); });
    assert.match(renderedText(lifecycle.renderer.toJSON()), /старая команда не повторялась/);
    assert.equal(renderedText(lifecycle.renderer.toJSON()).match(/Команда «Приостановить» выполнена/g)?.length ?? 0, 0);
  } finally { await lifecycle.cleanup(); }

  const deleting = await createHarness({
    detail: (_id, read) => goal({ version: read > 1 ? 9 : 3 }),
    onRequest: async () => { throw new ApiClientError("stale", "GOAL_VERSION_CONFLICT", 409); },
  });
  try {
    await openFirstGoal(deleting.renderer);
    await act(async () => { button(deleting.renderer, "Удалить").props.onClick(); });
    await act(async () => { button(deleting.renderer, "Удалить цель").props.onClick(); await settle(); });
    assert.equal(deleting.renderer.root.findAll((node) => renderedText(node.props.children).includes("Удалить цель «")).length, 0);
    assert.match(renderedText(deleting.renderer.toJSON()), /старая команда не повторялась/);
  } finally { await deleting.cleanup(); }

  const deleted = goal({ deleted_at: "2026-08-01T00:00:00Z", status: "completed", version: 3 });
  const restoring = await createHarness({
    detail: (_id, read) => ({ ...deleted, version: read > 1 ? 9 : 3 }),
    list: () => page([deleted]),
    onRequest: async () => { throw new ApiClientError("stale", "GOAL_VERSION_CONFLICT", 409); },
  });
  try {
    await openFirstGoal(restoring.renderer);
    await act(async () => { button(restoring.renderer, "Восстановить · Завершена").props.onClick(); await settle(); });
    assert.match(renderedText(restoring.renderer.toJSON()), /старая команда не повторялась/);
    assert.match(renderedText(restoring.renderer.root.findByProps({ "aria-label": "Детали цели" })), /Восстановить · Завершена/);
  } finally { await restoring.cleanup(); }
});

test("a Goal deleted by another client converges to read-only deleted state", async () => {
  const deleted = goal({ deleted_at: "2026-08-05T00:00:00Z", version: 4 });
  const harness = await createHarness({
    detail: (_id, read) => read === 1 ? goal() : deleted,
    onRequest: async () => { throw new ApiClientError("deleted", "GOAL_RESTORE_REQUIRED", 409); },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Приостановить").props.onClick(); await settle(); });
    const text = renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" }));
    assert.match(text, /Удалена/);
    assert.match(text, /Восстановить · Активна/);
    assert.doesNotMatch(text, /Приостановить|Добавить вклад|Исправить/);
  } finally { await harness.cleanup(); }
});

test("currency immutable rejection invalidates stale edit UI and refreshes contribution count", async () => {
  const stale = goal({ contribution_count: 0 });
  const fresh = goal({ contribution_count: 1, version: 4 });
  const harness = await createHarness({
    detail: (_id, read) => read === 1 ? stale : fresh,
    list: () => page([stale]),
    onRequest: async () => { throw new ApiClientError("locked", "GOAL_CURRENCY_IMMUTABLE", 409); },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Изменить").props.onClick(); });
    const edit = harness.renderer.root.findByProps({ "aria-label": "Редактирование цели" });
    await act(async () => { edit.findAllByType("input")[1].props.onChange({ target: { value: "USD" } }); });
    await act(async () => { edit.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(harness.renderer.root.findAllByProps({ "aria-label": "Редактирование цели" }).length, 0);
    assert.match(renderedText(harness.renderer.toJSON()), /Валюту нельзя изменить/);
    assert.match(renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" })), /Событий1/);
  } finally { await harness.cleanup(); }
});

test("confirmed contribution remains terminal when its fresh GET fails", async () => {
  const keys: string[] = [];
  const snapshot = goal({ contributed_amount: "50.0000", progress_percent: "50.0000", remaining_amount: "50.0000", version: 4 });
  const harness = await createHarness({
    detail: (_id, read) => read === 1 ? goal() : Promise.reject(new ApiClientError("offline", "API_NETWORK_ERROR", 0)),
    onRequest: async (_path, init) => {
      keys.push(new Headers(init.headers).get("X-Idempotency-Key") ?? "");
      return { contribution: contribution({ id: `event-${keys.length}` }), goal: snapshot };
    },
  });
  try {
    await openFirstGoal(harness.renderer);
    await act(async () => { button(harness.renderer, "Добавить вклад").props.onClick(); });
    let form = harness.renderer.root.findByType("form");
    await act(async () => { form.findByType("input").props.onChange({ target: { value: "25" } }); });
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(harness.renderer.root.findAllByType("form").length, 0);
    assert.match(renderedText(harness.renderer.toJSON()), /Команда выполнена, но актуальные данные загрузить не удалось/);
    assert.match(renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" })), /50%/);

    await act(async () => { button(harness.renderer, "Добавить вклад").props.onClick(); });
    form = harness.renderer.root.findByType("form");
    await act(async () => { form.findByType("input").props.onChange({ target: { value: "25" } }); });
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.notEqual(keys[0], keys[1]);
  } finally { await harness.cleanup(); }
});

test("confirmed create and delete do not become retryable mutations when refresh fails", async () => {
  const createKeys: string[] = [];
  let created = false;
  const createdGoal = goal({ currency: "USD", id: "created", name: "Новая цель", version: 1 });
  const creating = await createHarness({
    list: () => created
      ? Promise.reject(new ApiClientError("offline", "API_NETWORK_ERROR", 0))
      : page([]),
    onRequest: async (_path, init) => {
      createKeys.push(new Headers(init.headers).get("X-Idempotency-Key") ?? "");
      created = true;
      return createdGoal;
    },
  });
  try {
    await act(async () => { button(creating.renderer, "Создать цель").props.onClick(); });
    let editor = creating.renderer.root.findByProps({ "aria-label": "Создание цели" });
    await act(async () => { editor.findAllByType("input")[0].props.onChange({ target: { value: "Новая цель" } }); });
    editor = creating.renderer.root.findByProps({ "aria-label": "Создание цели" });
    await act(async () => { editor.findAllByType("input")[2].props.onChange({ target: { value: "100" } }); });
    await act(async () => { editor.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.equal(creating.renderer.root.findAllByProps({ "aria-label": "Создание цели" }).length, 0);
    assert.match(renderedText(creating.renderer.toJSON()), /Команда выполнена, но актуальные данные загрузить не удалось/);

    await act(async () => { button(creating.renderer, "＋ Создать цель").props.onClick(); });
    editor = creating.renderer.root.findByProps({ "aria-label": "Создание цели" });
    await act(async () => { editor.findAllByType("input")[0].props.onChange({ target: { value: "Другая цель" } }); });
    editor = creating.renderer.root.findByProps({ "aria-label": "Создание цели" });
    await act(async () => { editor.findAllByType("input")[2].props.onChange({ target: { value: "200" } }); });
    await act(async () => { editor.findByType("form").props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.notEqual(createKeys[0], createKeys[1]);
  } finally { await creating.cleanup(); }

  const live = goal();
  const deleted = goal({ deleted_at: "2026-08-05T00:00:00Z", version: 4 });
  const deleting = await createHarness({
    detail: (_id, read) => read === 1 ? live : Promise.reject(new ApiClientError("offline", "API_NETWORK_ERROR", 0)),
    list: () => page([live]),
    onRequest: async () => deleted,
  });
  try {
    await openFirstGoal(deleting.renderer);
    await act(async () => { button(deleting.renderer, "Удалить").props.onClick(); });
    await act(async () => { button(deleting.renderer, "Удалить цель").props.onClick(); await settle(); });
    assert.equal(deleting.renderer.root.findAllByType("button").filter((node) => renderedText(node.props.children) === "Удалить цель").length, 0);
    assert.match(renderedText(deleting.renderer.toJSON()), /Команда выполнена, но актуальные данные загрузить не удалось/);
    assert.match(renderedText(deleting.renderer.root.findByProps({ "aria-label": "Детали цели" })), /Восстановить · Активна/);
  } finally { await deleting.cleanup(); }
});

test("contribution identity is reset when moving from Goal A to Goal B", async () => {
  const a = goal({ id: "a", name: "Цель A" });
  const b = goal({ id: "b", name: "Цель B" });
  const requests: Array<{ key: string; path: string }> = [];
  const harness = await createHarness({
    detail: (id) => id === "a" ? a : b,
    list: () => page([a, b]),
    onRequest: async (path, init) => {
      requests.push({ key: new Headers(init.headers).get("X-Idempotency-Key") ?? "", path });
      if (requests.length === 1) throw new ApiClientError("offline", "API_NETWORK_ERROR", 0);
      return { contribution: contribution({ goal_id: "b", id: "event-b" }), goal: b };
    },
  });
  try {
    const openButtons = harness.renderer.root.findAllByType("button").filter((node) => renderedText(node.props.children) === "Открыть");
    await act(async () => { openButtons[0].props.onClick(); await settle(); });
    await act(async () => { button(harness.renderer, "Добавить вклад").props.onClick(); });
    let form = harness.renderer.root.findByType("form");
    await act(async () => { form.findByType("input").props.onChange({ target: { value: "10" } }); });
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    await act(async () => { button(harness.renderer, "Отмена").props.onClick(); });
    await act(async () => { harness.renderer.root.findByProps({ "aria-label": "Детали цели" }).findByProps({ "aria-label": "Закрыть" }).props.onClick(); });
    await act(async () => { openButtons[1].props.onClick(); await settle(); });
    await act(async () => { button(harness.renderer, "Добавить вклад").props.onClick(); });
    form = harness.renderer.root.findByType("form");
    await act(async () => { form.findByType("input").props.onChange({ target: { value: "10" } }); });
    await act(async () => { form.props.onSubmit({ preventDefault() {} }); await settle(); });
    assert.match(requests[0].path, /\/a\/contributions$/);
    assert.match(requests[1].path, /\/b\/contributions$/);
    assert.notEqual(requests[0].key, requests[1].key);
  } finally { await harness.cleanup(); }
});

test("closing a drawer invalidates pending detail and history without leaving history busy", async () => {
  let resolveDetail: ((value: Goal) => void) | undefined;
  const pendingDetail = new Promise<Goal>((resolve) => { resolveDetail = resolve; });
  const detailHarness = await createHarness({ detail: () => pendingDetail });
  try {
    await act(async () => { button(detailHarness.renderer, "Открыть").props.onClick(); await Promise.resolve(); });
    await act(async () => { detailHarness.renderer.root.findByProps({ "aria-label": "Детали цели" }).findByProps({ "aria-label": "Закрыть" }).props.onClick(); });
    await act(async () => { resolveDetail?.(goal({ name: "Поздняя цель" })); await settle(); });
    assert.equal(detailHarness.renderer.root.findAllByProps({ "aria-label": "Детали цели" }).length, 0);
  } finally { await detailHarness.cleanup(); }

  let resolveHistory: ((value: GoalContributionPage) => void) | undefined;
  const pendingHistory = new Promise<GoalContributionPage>((resolve) => { resolveHistory = resolve; });
  const historyHarness = await createHarness({ history: () => pendingHistory });
  try {
    await act(async () => { button(historyHarness.renderer, "Открыть").props.onClick(); await settle(); });
    await act(async () => { historyHarness.renderer.root.findByProps({ "aria-label": "Детали цели" }).findByProps({ "aria-label": "Закрыть" }).props.onClick(); });
    await act(async () => { resolveHistory?.(historyPage([contribution()])); await settle(); });
    await openFirstGoal(historyHarness.renderer);
    assert.equal(button(historyHarness.renderer, "Добавить вклад").props.disabled, false);
    assert.equal(
      historyHarness.renderer.root.findAll((node) => renderedText(node.props.children) === "Загружаем историю…").length,
      0,
    );
    await act(async () => { historyHarness.renderer.root.findByProps({ "aria-label": "Детали цели" }).findByProps({ "aria-label": "Закрыть" }).props.onClick(); });
  } finally { await historyHarness.cleanup(); }
});

test("late Goal A detail cannot replace the currently selected Goal B", async () => {
  let resolveA: ((value: Goal) => void) | undefined;
  const detailA = new Promise<Goal>((resolve) => { resolveA = resolve; });
  const a = goal({ id: "a", name: "Цель A" });
  const b = goal({ id: "b", name: "Цель B" });
  const harness = await createHarness({ detail: (id) => id === "a" ? detailA : b, list: () => page([a, b]) });
  try {
    const openButtons = harness.renderer.root.findAllByType("button").filter((node) => renderedText(node.props.children) === "Открыть");
    await act(async () => { openButtons[0].props.onClick(); await Promise.resolve(); });
    await act(async () => { openButtons[1].props.onClick(); await settle(); });
    assert.match(renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" })), /Цель B/);
    await act(async () => { resolveA?.(a); await settle(); });
    const text = renderedText(harness.renderer.root.findByProps({ "aria-label": "Детали цели" }));
    assert.match(text, /Цель B/);
    assert.doesNotMatch(text, /Цель A/);
  } finally { await harness.cleanup(); }
});
