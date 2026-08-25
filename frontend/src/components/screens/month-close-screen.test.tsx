import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { apiClient, ApiClientError } from "@/lib/api-client";
import type {
  MonthCloseAsClosedReport,
  MonthCloseComparison,
  MonthCloseHistoryPage,
  MonthCloseIssue,
  MonthCloseRevision,
  MonthClosure,
  MonthClosurePage,
} from "@/types/automations";

import { MonthCloseScreen } from "./month-close-screen";

function capabilities(overrides: Partial<MonthClosure["capabilities"]> = {}): MonthClosure["capabilities"] {
  return { can_confirm: false, can_prepare: false, can_reopen: false, can_view_history: true, ...overrides };
}

const warning: MonthCloseIssue = {
  code: "ACCOUNT_NOT_RECONCILED",
  count: 1,
  details: { account_ids: ["account-1"] },
  message: "Not all material accounts have reconciliation evidence through period end.",
  scope: "reconciliation",
  severity: "warning",
};

const blocker: MonthCloseIssue = {
  code: "DRAFT_TRANSACTIONS",
  count: 2,
  details: {},
  message: "The closing interval contains draft transactions.",
  scope: "closing_interval",
  severity: "blocker",
};

const summary = {
  account_balances: [{ account_id: "account-1", balance: "135.0000", currency: "RUB", name: "Основной", opening_balance: "100.0000" }],
  backup_policy: "warn",
  backup_status: "unverified",
  backup_verified_at: null,
  currencies: [{ adjustment: "5.0000", currency: "RUB", expense: "20.0000", income: "50.0000", net_cashflow: "35.0000", transactions_count: 3, transfer_volume: "10.0000" }],
  reconciliation_coverage: [{ account_id: "account-1", account_name: "Основной", account_type: "debit_card", archived: false, covered: false, currency: "RUB", latest_statement_date: null, required_statement_date: "2026-07-31", state: "not_reconciled" }],
  transaction_count: 3,
};

function closure(overrides: Partial<MonthClosure> = {}): MonthClosure {
  return {
    blocking_issues: [],
    capabilities: capabilities({ can_confirm: true, can_prepare: true }),
    confirmed_at: null,
    current_revision: null,
    current_revision_id: null,
    id: "closure-1",
    info_issues: [],
    last_reopen_reason: null,
    last_reopened_at: null,
    last_reopened_by: null,
    period_month: "2026-07-01",
    prepare_token: "a".repeat(64),
    prepared_at: "2026-08-01T10:00:00Z",
    prepared_fingerprint: "b".repeat(64),
    status: "ready",
    summary,
    version: 2,
    warning_issues: [warning],
    ...overrides,
  };
}

function page(item: MonthClosure | null, overrides: Partial<MonthClosurePage> = {}): MonthClosurePage {
  const status = item?.status ?? "not_prepared";
  return {
    backup_policy: "warn",
    closed_through: null,
    items: item ? [item] : [],
    page: { limit: 120, offset: 0, total: item ? 1 : 0 },
    periods: [{
      blocker_count: item?.blocking_issues?.length ?? 0,
      capabilities: item?.capabilities ?? capabilities({ can_prepare: true }),
      confirmed_at: item?.confirmed_at ?? null,
      current_revision: item?.current_revision ?? null,
      period_month: "2026-07-01",
      prepared: Boolean(item?.prepared_at),
      reopened_at: item?.last_reopened_at ?? null,
      status,
      version: item?.version ?? null,
      warning_count: item?.warning_issues?.length ?? 0,
    }],
    ...overrides,
  };
}

const revision: MonthCloseRevision = {
  confirmed_at: "2026-08-01T12:00:00Z",
  confirmed_by: { display_name: "Никита", display_name_source: "current_profile", id: "user-1" },
  financial_fingerprint: "c".repeat(64),
  id: "revision-1",
  legacy_unverified: false,
  period_end_at: "2026-07-31T19:00:00Z",
  period_month: "2026-07-01",
  period_start_at: "2026-06-30T19:00:00Z",
  reopened: null,
  revision_number: 1,
  snapshot_summary: summary,
  source: "api",
};

function historyPage(items: MonthCloseRevision[], item = confirmedClosure): MonthCloseHistoryPage {
  return { closure: item, items, order: "newest", page: { limit: 100, offset: 0, total: items.length } };
}

const confirmedClosure = closure({
  capabilities: capabilities({ can_reopen: true }),
  confirmed_at: "2026-08-01T12:00:00Z",
  current_revision: 1,
  current_revision_id: "revision-1",
  status: "confirmed",
  version: 3,
});

const report: MonthCloseAsClosedReport = {
  account_balances: summary.account_balances,
  category_aggregates: [],
  confirmed_at: revision.confirmed_at,
  confirmed_by: revision.confirmed_by,
  currencies: summary.currencies,
  financial_fingerprint: revision.financial_fingerprint,
  issue_summary: { blocker_count: 0, blockers: [], info: [], info_count: 0, warning_count: 1, warnings: [warning] },
  legacy_unverified: false,
  mode: "as_closed",
  period: { month: "2026-07-01" },
  reconciliation_coverage: summary.reconciliation_coverage,
  revision_number: 1,
  transaction_count: 3,
  unavailable_sections: [],
};

const comparison: MonthCloseComparison = {
  as_closed: report,
  current: { currencies: [{ ...summary.currencies[0], expense: "25.0000", net_cashflow: "30.0000" }] },
  differences: { account_balances: [], category_aggregates: [], currencies: [{ changed: true, currency: "RUB" }] },
  period_month: "2026-07-01",
  revision_number: 1,
  unavailable_sections: [],
};

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await new Promise<void>((resolve) => setImmediate(resolve));
}

function findLastByProps(renderer: ReactTestRenderer, props: Record<string, unknown>) {
  const matches = renderer.root.findAllByProps(props);
  const match = matches.at(-1);
  if (!match) throw new Error(`No rendered node matched ${JSON.stringify(props)}`);
  return match;
}

function renderedText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(renderedText).join("");
  if (value && typeof value === "object" && "children" in value) {
    return renderedText((value as { children?: unknown }).children);
  }
  return "";
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

test("month close finishes loading and renders honest empty and blocked states", async () => {
  const originalGet = apiClient.get;
  const restore = installBrowserGlobals();
  let resolvePage: ((value: MonthClosurePage) => void) | undefined;
  let renderer: ReactTestRenderer | undefined;
  const errors: unknown[] = [];
  apiClient.get = (() => new Promise<MonthClosurePage>((resolve) => { resolvePage = resolve; })) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={(error) => errors.push(error)}/>); });
    assert.match(JSON.stringify(renderer?.toJSON()), /Загружаем закрытие месяца/);
    await act(async () => { resolvePage?.(page(null)); await settle(); });
    const empty = JSON.stringify(renderer?.toJSON());
    assert.match(empty, /Подготовленных месяцев нет/);
    assert.match(empty, /Подготовить/);
    assert.equal(errors.length, 0);

    apiClient.get = (() => Promise.resolve(page(closure({ blocking_issues: [blocker], capabilities: capabilities({ can_prepare: true }), status: "blocked" })))) as typeof apiClient.get;
    await act(async () => { renderer?.unmount(); renderer = create(<MonthCloseScreen onError={(error) => errors.push(error)}/>); await settle(); });
    const blocked = JSON.stringify(renderer?.toJSON());
    assert.match(blocked, /Заблокирован/);
    assert.match(blocked, /Черновики операций/);
    assert.doesNotMatch(blocked, /Подтвердить закрытие/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restore();
  }
});

test("editor prepares an empty period and reopened state does not expose owner actions", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restore = installBrowserGlobals();
  let currentPage = page(null);
  const posts: string[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => Promise.resolve(
    (path.includes("/history?") ? historyPage([]) : currentPage) as T,
  )) as typeof apiClient.get;
  apiClient.post = (<T,>(path: string) => {
    posts.push(path);
    const prepared = closure({
      capabilities: capabilities({ can_prepare: true }),
      warning_issues: [],
    });
    currentPage = page(prepared);
    return Promise.resolve(prepared as T);
  }) as typeof apiClient.post;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={() => undefined}/>); await settle(); });
    if (!renderer) throw new Error("Renderer was not created");
    await act(async () => { renderer?.root.findByProps({ children: "Подготовить" }).props.onClick(); await settle(); });
    assert.deepEqual(posts, ["/api/v1/month-close/2026/7/prepare"]);
    let output = renderedText(renderer.toJSON());
    assert.match(output, /Preview можно обновить/);
    assert.doesNotMatch(output, /Подтвердить закрытие|Открыть повторно/);

    const reopened = closure({
      capabilities: capabilities({ can_prepare: true }),
      current_revision: 1,
      current_revision_id: "revision-1",
      status: "reopened",
    });
    currentPage = page(reopened);
    await act(async () => { renderer?.unmount(); renderer = create(<MonthCloseScreen onError={() => undefined}/>); await settle(); });
    output = renderedText(renderer?.toJSON());
    assert.match(output, /Период открыт для поправок/);
    assert.doesNotMatch(output, /Подтвердить закрытие|Открыть повторно/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restore();
  }
});

test("load error is reported and recovers only after an explicit retry", async () => {
  const originalGet = apiClient.get;
  const restore = installBrowserGlobals();
  const errors: unknown[] = [];
  let calls = 0;
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>() => {
    calls += 1;
    return calls === 1
      ? Promise.reject(new ApiClientError("network", "API_NETWORK_ERROR", 0))
      : Promise.resolve(page(null) as T);
  }) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={(error) => errors.push(error)}/>); await settle(); });
    assert.match(renderedText(renderer?.toJSON()), /Не удалось загрузить состояние закрытия месяца/);
    assert.equal(errors.length, 1);
    await act(async () => { renderer?.root.findByProps({ children: "Повторить" }).props.onClick(); await settle(); });
    assert.equal(calls, 2);
    assert.match(renderedText(renderer?.toJSON()), /Подготовленных месяцев нет/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restore();
  }
});

test("confirm modal reuses one idempotency key after transport failure and does not auto retry", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restore = installBrowserGlobals();
  let currentPage = page(closure());
  const requests: Array<{ headers?: HeadersInit; path: string }> = [];
  const errors: unknown[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => Promise.resolve((path.includes("/history?") ? historyPage([]) : currentPage) as T)) as typeof apiClient.get;
  apiClient.post = (<T,>(path: string, _body?: unknown, headers?: HeadersInit) => {
    requests.push({ headers, path });
    if (requests.length === 1) return Promise.reject(new ApiClientError("network", "API_NETWORK_ERROR", 0));
    currentPage = page(confirmedClosure, { closed_through: "2026-07-31" });
    return Promise.resolve(confirmedClosure as T);
  }) as typeof apiClient.post;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={(error) => errors.push(error)}/>); await settle(); });
    if (!renderer) throw new Error("Renderer was not created");
    await act(async () => { renderer?.root.findByProps({ children: "Подтвердить закрытие" }).props.onClick(); });
    assert.match(renderedText(renderer.toJSON()), /Операции до 2026-07-31 будут защищены от изменения/);
    const confirmButton = findLastByProps(renderer, { children: "Подтвердить закрытие" });
    await act(async () => { confirmButton.props.onClick(); await settle(); });
    assert.equal(requests.length, 1);
    assert.equal(errors.length, 1);
    await act(async () => { if (renderer) findLastByProps(renderer, { children: "Подтвердить закрытие" }).props.onClick(); await settle(); });
    assert.equal(requests.length, 2);
    const firstKey = (requests[0].headers as Record<string, string>)["X-Idempotency-Key"];
    const secondKey = (requests[1].headers as Record<string, string>)["X-Idempotency-Key"];
    assert.equal(firstKey, secondKey);
    assert.match(firstKey, /^month-close-confirm-/);
    assert.match(JSON.stringify(renderer.toJSON()), /Период защищён/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restore();
  }
});

test("cancelled confirm dialog starts a new intent with a new key", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restore = installBrowserGlobals();
  const keys: string[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => Promise.resolve(
    (path.includes("/history?") ? historyPage([]) : page(closure())) as T,
  )) as typeof apiClient.get;
  apiClient.post = (<T,>(_path: string, _body?: unknown, headers?: HeadersInit) => {
    keys.push((headers as Record<string, string>)["X-Idempotency-Key"]);
    return Promise.reject(new ApiClientError("network", "API_NETWORK_ERROR", 0)) as Promise<T>;
  }) as typeof apiClient.post;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={() => undefined}/>); await settle(); });
    if (!renderer) throw new Error("Renderer was not created");
    await act(async () => { renderer?.root.findByProps({ children: "Подтвердить закрытие" }).props.onClick(); });
    await act(async () => { findLastByProps(renderer!, { children: "Подтвердить закрытие" }).props.onClick(); await settle(); });
    await act(async () => { renderer?.root.findByProps({ children: "Отмена" }).props.onClick(); });
    await act(async () => { renderer?.root.findByProps({ children: "Подтвердить закрытие" }).props.onClick(); });
    await act(async () => { findLastByProps(renderer!, { children: "Подтвердить закрытие" }).props.onClick(); await settle(); });
    assert.equal(keys.length, 2);
    assert.notEqual(keys[0], keys[1]);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restore();
  }
});

test("reopen modal requires a reason, keeps its key on retry, and viewer actions stay hidden", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restore = installBrowserGlobals();
  let currentPage = page(confirmedClosure, { closed_through: "2026-07-31" });
  const keys: string[] = [];
  let calls = 0;
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => Promise.resolve((path.includes("/history?") ? historyPage([revision]) : currentPage) as T)) as typeof apiClient.get;
  apiClient.post = (<T,>(_path: string, _body?: unknown, headers?: HeadersInit) => {
    calls += 1;
    keys.push((headers as Record<string, string>)["X-Idempotency-Key"]);
    if (calls === 1) return Promise.reject(new ApiClientError("network", "API_NETWORK_ERROR", 0));
    const reopened = closure({ capabilities: capabilities({ can_prepare: true }), current_revision: 1, current_revision_id: "revision-1", status: "reopened", version: 4 });
    currentPage = page(reopened, { closed_through: null });
    return Promise.resolve(reopened as T);
  }) as typeof apiClient.post;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={() => undefined}/>); await settle(); });
    if (!renderer) throw new Error("Renderer was not created");
    await act(async () => { renderer?.root.findByProps({ children: "Открыть повторно" }).props.onClick(); });
    const reopenButton = renderer.root.findByProps({ children: "Открыть месяц" });
    assert.equal(reopenButton.props.disabled, true);
    await act(async () => { renderer?.root.findByType("textarea").props.onChange({ target: { value: "Поздняя корректировка" } }); });
    assert.equal(renderer.root.findByProps({ children: "Открыть месяц" }).props.disabled, false);
    await act(async () => { renderer?.root.findByProps({ children: "Открыть месяц" }).props.onClick(); await settle(); });
    await act(async () => { renderer?.root.findByProps({ children: "Открыть месяц" }).props.onClick(); await settle(); });
    assert.equal(calls, 2);
    assert.equal(keys[0], keys[1]);
    assert.match(keys[0], /^month-close-reopen-/);

    const viewer = closure({ capabilities: capabilities(), current_revision: 1, current_revision_id: "revision-1", status: "confirmed" });
    currentPage = page(viewer);
    await act(async () => { renderer?.unmount(); renderer = create(<MonthCloseScreen onError={() => undefined}/>); await settle(); });
    const output = renderedText(renderer?.toJSON());
    assert.doesNotMatch(output, /Подтвердить закрытие/);
    assert.doesNotMatch(output, /Открыть повторно/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restore();
  }
});

test("reopen payload change after a business rejection gets a new key", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restore = installBrowserGlobals();
  const requests: Array<{ body: unknown; key: string }> = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => Promise.resolve(
    (path.includes("/history?") ? historyPage([revision]) : page(confirmedClosure, { closed_through: "2026-07-31" })) as T,
  )) as typeof apiClient.get;
  apiClient.post = (<T,>(_path: string, body?: unknown, headers?: HeadersInit) => {
    requests.push({ body, key: (headers as Record<string, string>)["X-Idempotency-Key"] });
    return Promise.reject(new ApiClientError("reason rejected", "VALIDATION_ERROR", 422)) as Promise<T>;
  }) as typeof apiClient.post;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={() => undefined}/>); await settle(); });
    await act(async () => { renderer?.root.findByProps({ children: "Открыть повторно" }).props.onClick(); });
    await act(async () => { renderer?.root.findByType("textarea").props.onChange({ target: { value: "Причина A" } }); });
    await act(async () => { renderer?.root.findByProps({ children: "Открыть месяц" }).props.onClick(); await settle(); });
    await act(async () => { renderer?.root.findByType("textarea").props.onChange({ target: { value: "Причина B" } }); });
    await act(async () => { renderer?.root.findByProps({ children: "Открыть месяц" }).props.onClick(); await settle(); });
    assert.equal(requests.length, 2);
    assert.notEqual(requests[0].key, requests[1].key);
    assert.deepEqual(requests.map((item) => item.body), [
      { reason: "Причина A", version: 3 },
      { reason: "Причина B", version: 3 },
    ]);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restore();
  }
});

test("stale and version conflicts refetch without automatic mutation retry", async () => {
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const restore = installBrowserGlobals();
  let getCalls = 0;
  let postCalls = 0;
  const keys: string[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>() => { getCalls += 1; return Promise.resolve(page(closure()) as T); }) as typeof apiClient.get;
  apiClient.post = ((_path: string, _body?: unknown, headers?: HeadersInit) => {
    postCalls += 1;
    keys.push((headers as Record<string, string>)["X-Idempotency-Key"]);
    return Promise.reject(new ApiClientError("stale", "MONTH_CLOSE_PREVIEW_STALE", 409));
  }) as typeof apiClient.post;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={() => undefined}/>); await settle(); });
    await act(async () => { renderer?.root.findByProps({ children: "Подтвердить закрытие" }).props.onClick(); });
    await act(async () => { if (renderer) findLastByProps(renderer, { children: "Подтвердить закрытие" }).props.onClick(); await settle(); });
    assert.equal(postCalls, 1);
    assert.ok(getCalls >= 2);
    assert.match(JSON.stringify(renderer?.toJSON()), /выполните подготовку заново/);

    apiClient.post = ((_path: string, _body?: unknown, headers?: HeadersInit) => {
      postCalls += 1;
      keys.push((headers as Record<string, string>)["X-Idempotency-Key"]);
      return Promise.reject(new ApiClientError("version", "MONTH_CLOSE_VERSION_CONFLICT", 409));
    }) as typeof apiClient.post;
    await act(async () => { renderer?.root.findByProps({ children: "Подтвердить закрытие" }).props.onClick(); });
    await act(async () => { if (renderer) findLastByProps(renderer, { children: "Подтвердить закрытие" }).props.onClick(); await settle(); });
    assert.equal(postCalls, 2);
    assert.notEqual(keys[0], keys[1]);
    assert.match(JSON.stringify(renderer?.toJSON()), /Версия закрытия устарела/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restore();
  }
});

test("history renders legacy warning and separates current from as-closed multi-currency data", async () => {
  const originalGet = apiClient.get;
  const restore = installBrowserGlobals();
  const legacyRevision = { ...revision, financial_fingerprint: null, legacy_unverified: true };
  const legacyReport = { ...report, financial_fingerprint: null, legacy_unverified: true };
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    if (path === "/api/v1/month-close?limit=120") return Promise.resolve(page(confirmedClosure, { closed_through: "2026-07-31" }) as T);
    if (path.includes("/history?") ) return Promise.resolve(historyPage([legacyRevision]) as T);
    if (path.endsWith("/report")) return Promise.resolve(legacyReport as T);
    if (path.endsWith("/comparison")) return Promise.resolve(comparison as T);
    throw new Error(`Unexpected request: ${path}`);
  }) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={(error) => { throw error; }}/>); await settle(); });
    assert.match(JSON.stringify(renderer?.toJSON()), /Legacy unverified/);
    await act(async () => { renderer?.root.findByProps({ children: "Открыть снимок →" }).props.onClick(); await settle(); });
    const output = renderedText(renderer?.toJSON());
    assert.match(output, /Историческое закрытие создано до введения проверяемых снимков/);
    assert.match(output, /Закрыто в revision 1/);
    assert.match(output, /Текущие данные/);
    assert.match(output, /Текущие итоги отличаются/);
    assert.match(output, /Валюты не складываются/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restore();
  }
});

test("legacy snapshot with missing sections shows unavailable instead of fake zeroes", async () => {
  const originalGet = apiClient.get;
  const restore = installBrowserGlobals();
  const legacyRevision = { ...revision, financial_fingerprint: null, legacy_unverified: true };
  const missingSections = [
    "account_balances",
    "category_aggregates",
    "currencies",
    "issue_summary",
    "reconciliation_coverage",
    "transaction_count",
  ];
  const legacyReport: MonthCloseAsClosedReport = {
    ...report,
    account_balances: null,
    category_aggregates: null,
    currencies: null,
    financial_fingerprint: null,
    issue_summary: null,
    legacy_unverified: true,
    reconciliation_coverage: null,
    transaction_count: null,
    unavailable_sections: missingSections,
  };
  const legacyComparison: MonthCloseComparison = {
    ...comparison,
    as_closed: legacyReport,
    differences: { account_balances: [], category_aggregates: [], currencies: [] },
    unavailable_sections: missingSections,
  };
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    if (path === "/api/v1/month-close?limit=120") return Promise.resolve(page(confirmedClosure) as T);
    if (path.includes("/history?")) return Promise.resolve(historyPage([legacyRevision]) as T);
    if (path.endsWith("/report")) return Promise.resolve(legacyReport as T);
    if (path.endsWith("/comparison")) return Promise.resolve(legacyComparison as T);
    throw new Error(`Unexpected request: ${path}`);
  }) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={(error) => { throw error; }}/>); await settle(); });
    await act(async () => { renderer?.root.findByProps({ children: "Открыть снимок →" }).props.onClick(); await settle(); });
    const output = renderedText(renderer?.toJSON());
    assert.match(output, /Историческое закрытие создано/);
    assert.match(output, /Недоступно/);
    assert.match(output, /Сравнение валютных итогов недоступно/);
    assert.doesNotMatch(output, /Financial fingerprint|Валютные итоги совпадают|Текущие итоги отличаются/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restore();
  }
});

test("out-of-order history and revision responses cannot cross month boundaries", async () => {
  const originalGet = apiClient.get;
  const restore = installBrowserGlobals();
  const julyClosure = confirmedClosure;
  const augustClosure = closure({
    capabilities: capabilities({ can_reopen: true }),
    confirmed_at: "2026-09-01T12:00:00Z",
    current_revision: 2,
    current_revision_id: "revision-august",
    period_month: "2026-08-01",
    status: "confirmed",
    version: 4,
  });
  const julyRevision = {
    ...revision,
    confirmed_by: { display_name: "July actor", display_name_source: "current_profile" as const, id: "july-user" },
  };
  const augustRevision = {
    ...revision,
    confirmed_at: "2026-09-01T12:00:00Z",
    confirmed_by: { display_name: "August actor", display_name_source: "current_profile" as const, id: "august-user" },
    id: "revision-august",
    period_end_at: "2026-08-31T19:00:00Z",
    period_month: "2026-08-01",
    period_start_at: "2026-07-31T19:00:00Z",
    revision_number: 2,
  };
  const julyPeriod = page(julyClosure).periods[0];
  const augustPeriod = {
    ...julyPeriod,
    confirmed_at: augustClosure.confirmed_at,
    current_revision: 2,
    period_month: "2026-08-01",
    status: "confirmed" as const,
    version: 4,
  };
  const multiPage = page(julyClosure, {
    items: [julyClosure, augustClosure],
    periods: [augustPeriod, julyPeriod],
  });
  let resolveFirstJuly: ((value: MonthCloseHistoryPage) => void) | undefined;
  let julyHistoryCalls = 0;
  let resolveAugustReport: ((value: MonthCloseAsClosedReport) => void) | undefined;
  let resolveAugustComparison: ((value: MonthCloseComparison) => void) | undefined;
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    if (path === "/api/v1/month-close?limit=120") return Promise.resolve(multiPage as T);
    if (path.includes("/2026/7/history?")) {
      julyHistoryCalls += 1;
      if (julyHistoryCalls === 1) {
        return new Promise<MonthCloseHistoryPage>((resolve) => { resolveFirstJuly = resolve; }) as Promise<T>;
      }
      return Promise.resolve(historyPage([julyRevision], julyClosure) as T);
    }
    if (path.includes("/2026/8/history?")) return Promise.resolve(historyPage([augustRevision], augustClosure) as T);
    if (path.endsWith("/history/2/report")) {
      return new Promise<MonthCloseAsClosedReport>((resolve) => { resolveAugustReport = resolve; }) as Promise<T>;
    }
    if (path.endsWith("/history/2/comparison")) {
      return new Promise<MonthCloseComparison>((resolve) => { resolveAugustComparison = resolve; }) as Promise<T>;
    }
    throw new Error(`Unexpected request: ${path}`);
  }) as typeof apiClient.get;
  try {
    await act(async () => { renderer = create(<MonthCloseScreen onError={(error) => { throw error; }}/>); await settle(); });
    await act(async () => { renderer?.root.findByProps({ type: "month" }).props.onChange({ target: { value: "2026-08" } }); await settle(); });
    let historyText = renderedText(renderer?.root.findByProps({ className: "month-close-history-list" }));
    assert.match(historyText, /August actor/);
    await act(async () => { resolveFirstJuly?.(historyPage([julyRevision], julyClosure)); await settle(); });
    historyText = renderedText(renderer?.root.findByProps({ className: "month-close-history-list" }));
    assert.match(historyText, /August actor/);
    assert.doesNotMatch(historyText, /July actor/);

    await act(async () => { renderer?.root.findByProps({ children: "Открыть снимок →" }).props.onClick(); });
    await act(async () => { renderer?.root.findByProps({ type: "month" }).props.onChange({ target: { value: "2026-07" } }); await settle(); });
    await act(async () => {
      resolveAugustReport?.({ ...report, confirmed_at: augustRevision.confirmed_at, revision_number: 2 });
      resolveAugustComparison?.({ ...comparison, revision_number: 2 });
      await settle();
    });
    assert.equal(renderer?.root.findAllByProps({ "aria-label": "Исторический снимок закрытия месяца" }).length, 0);
    historyText = renderedText(renderer?.root.findByProps({ className: "month-close-history-list" }));
    assert.match(historyText, /July actor/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    restore();
  }
});
