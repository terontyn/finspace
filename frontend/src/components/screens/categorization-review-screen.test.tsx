import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { ApiClientError, apiClient, type WorkspaceRole } from "@/lib/api-client";
import type { CategorizationReviewImportScope } from "@/lib/categorization-review-scope";
import { canonicalItemIds, nextApplyAttempt, sameItemSet } from "@/lib/categorization-review";

import type {
  CategorizationApplyResponse,
  CategorizationApplyStatus,
  CategorizationPreviewHeader,
  CategorizationPreviewItem,
  CategorizationPreviewItemPage,
  CategorizationPreviewStatus,
} from "@/types/categorization";
import type { Account, Paged, Payee } from "@/types/finance";

import { CategorizationReviewScreen } from "./categorization-review-screen";

const account: Account = { account_type: "debit_card", created_at: "2026-08-01T00:00:00Z", credit_limit: null, currency: "RUB", description: null, id: "account-1", institution: "Банк", is_archived: false, name: "Основной", opening_balance: "0.0000", opening_balance_at: "2026-08-01", updated_at: "2026-08-01T00:00:00Z", version: 1 };
const payee: Payee = { aliases: [], created_at: "2026-08-01T00:00:00Z", deleted_at: null, id: "payee-1", name: "Кофейня", notes: null, updated_at: "2026-08-01T00:00:00Z", version: 1 };

function page<T>(items: T[], total = items.length, offset = 0, limit = 50): Paged<T> {
  return { items, page: { limit, offset, total } };
}

function header(overrides: Partial<CategorizationPreviewHeader> = {}): CategorizationPreviewHeader {
  return {
    created_at: "2026-08-20T10:00:00Z",
    created_by: "user-1",
    expires_at: "2026-08-21T10:00:00Z",
    id: "preview-1",
    rule_set_version: 4,
    selection_mode: "filter",
    summary: { already_categorized: 1, closed_period: 0, matched: 2, no_match: 1, not_found: 0, reconciled: 0, selected: 4, split: 0, transfer: 0 },
    workspace_id: "workspace-1",
    ...overrides,
  };
}

function item(id: string, status: CategorizationPreviewStatus, overrides: Partial<CategorizationPreviewItem> = {}): CategorizationPreviewItem {
  return {
    category_id: status === "matched" ? "category-1" : null,
    category_name: status === "matched" ? "Кафе" : null,
    category_version: status === "matched" ? 1 : null,
    id,
    rule_id: status === "matched" ? "rule-1" : null,
    rule_name: status === "matched" ? "Кофе" : null,
    rule_version: status === "matched" ? 1 : null,
    sequence: 0,
    status,
    transaction: {
      account_id: "account-1",
      amount: "1250.2500",
      counterparty: `Контрагент ${id}`,
      currency: "RUB",
      description: null,
      occurred_at: "2026-08-15T12:00:00Z",
      payee_id: "payee-1",
      source: "import",
      status: "confirmed",
      transaction_id: `transaction-${id}`,
      transaction_type: "expense",
      version: 1,
    },
    transaction_id: `transaction-${id}`,
    transaction_version: 1,
    ...overrides,
  };
}

function applyResponse(statuses: CategorizationApplyStatus[], itemIds: string[]): CategorizationApplyResponse {
  const results = itemIds.map((itemId, index) => ({
    current_version: null,
    error_code: null,
    expected_version: 1,
    item_id: itemId,
    status: statuses[index] ?? "applied",
    transaction_id: `transaction-${itemId}`,
    transaction_version: null,
  }));
  const applied = results.filter((result) => result.status === "applied").length;
  const conflicts = results.filter((result) => ["category_changed", "rule_changed", "transaction_changed"].includes(result.status)).length;
  const failed = results.filter((result) => result.status === "failed").length;
  return {
    operation_id: "operation-1",
    preview_id: "preview-1",
    results,
    summary: { applied, conflicts, failed, not_applied: results.length - applied - conflicts - failed, requested: results.length },
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

function findButton(root: ReactTestInstance, label: string): ReactTestInstance | undefined {
  return root.findAllByType("button").find((node) => renderedText(node.props.children).includes(label));
}

function button(root: ReactTestInstance, label: string): ReactTestInstance {
  const match = findButton(root, label);
  if (!match) throw new Error(`Кнопка не найдена: ${label}`);
  return match;
}

function checkboxes(root: ReactTestInstance): ReactTestInstance[] {
  return root.findAllByType("input").filter((node) => node.props.type === "checkbox");
}

interface ApplyCall { body: unknown; headers: unknown; path: string }
interface CreateCall { body: unknown; path: string }

interface HarnessOptions {
  importScope?: CategorizationReviewImportScope;
  items?: CategorizationPreviewItem[];
  itemsPages?: CategorizationPreviewItemPage[];
  onApply?: (call: ApplyCall) => unknown;
  onCreate?: (call: CreateCall) => unknown;
  role?: WorkspaceRole;
}

/** React and next/link expect a browser-ish global surface; mirrors the shell test harness. */
function installBrowserGlobals(): () => void {
  const descriptors = new Map<string, PropertyDescriptor | undefined>();
  for (const key of ["window", "self", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) descriptors.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  class TestHTMLElement { focus() {} }
  const browser = { addEventListener: () => undefined, cancelAnimationFrame: () => undefined, clearTimeout: globalThis.clearTimeout.bind(globalThis), removeEventListener: () => undefined, requestAnimationFrame: (callback: () => void) => { callback(); return 1; }, setTimeout: globalThis.setTimeout.bind(globalThis) };
  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: TestHTMLElement });
  Object.defineProperty(globalThis, "document", { configurable: true, value: { activeElement: null } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: browser });
  Object.defineProperty(globalThis, "self", { configurable: true, value: browser });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });
  return () => { for (const [key, descriptor] of descriptors) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key); } };
}

async function createHarness(options: HarnessOptions = {}) {
  const restoreBrowser = installBrowserGlobals();
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const applyCalls: ApplyCall[] = [];
  const createCalls: CreateCall[] = [];
  const getCalls: string[] = [];
  const itemRequests: string[] = [];
  let itemsPageIndex = 0;

  apiClient.get = (<T,>(path: string) => {
    getCalls.push(path);
    if (path.startsWith("/api/v1/accounts")) return Promise.resolve(page([account]) as T);
    if (path.startsWith("/api/v1/payees")) return Promise.resolve(page([payee]) as T);
    if (path.includes("/items")) {
      itemRequests.push(path);
      if (options.itemsPages) {
        const next = options.itemsPages[Math.min(itemsPageIndex, options.itemsPages.length - 1)];
        itemsPageIndex += 1;
        return Promise.resolve(next as T);
      }
      const items = options.items ?? [];
      return Promise.resolve({ items, page: { limit: 50, offset: 0, total: items.length } } as T);
    }
    return Promise.reject(new Error(`Неожиданный GET ${path}`));
  }) as typeof apiClient.get;

  apiClient.post = (<T,>(path: string, body?: unknown, headers?: HeadersInit) => {
    if (path.endsWith("/apply")) {
      const call = { body, headers, path };
      applyCalls.push(call);
      const outcome = options.onApply?.(call);
      if (outcome instanceof Error) return Promise.reject(outcome);
      return Promise.resolve((outcome ?? applyResponse(["applied"], (body as { item_ids: string[] }).item_ids)) as T);
    }
    const call = { body, path };
    createCalls.push(call);
    const created = options.onCreate?.(call);
    if (created instanceof Error) return Promise.reject(created);
    return Promise.resolve((created ?? header()) as T);
  }) as typeof apiClient.post;

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<CategorizationReviewScreen importScope={options.importScope} onError={() => {}} role={options.role ?? "editor"} roleLoading={false} />);
  });
  await act(async () => { await settle(); });

  return {
    applyCalls,
    createCalls,
    getCalls,
    itemRequests,
    get renderer() { return renderer as ReactTestRenderer; },
    get root() { return (renderer as ReactTestRenderer).root; },
    async cleanup() {
      await act(async () => renderer?.unmount());
      apiClient.get = originalGet;
      apiClient.post = originalPost;
      restoreBrowser();
    },
    async createPreview() {
      await act(async () => { button(this.root, "Составить список").props.onClick?.({ preventDefault() {} }); });
      await act(async () => { await settle(); });
    },
  };
}

async function submitFilters(harness: Awaited<ReturnType<typeof createHarness>>) {
  const form = harness.root.findByType("form");
  await act(async () => { form.props.onSubmit({ preventDefault() {} }); });
  await act(async () => { await settle(); });
}

async function toggleRow(harness: Awaited<ReturnType<typeof createHarness>>, index: number) {
  const boxes = checkboxes(harness.root);
  await act(async () => { boxes[index]?.props.onChange(); });
  await act(async () => { await settle(); });
}

const scopedBatchId = "152eabf4-4447-4f22-8ada-95647ff80f30";

test("import scope is explicit, retained with narrower filters, and does not auto-preview", async () => {
  const harness = await createHarness({
    importScope: { kind: "valid", importBatchId: scopedBatchId },
    items: [item("a", "matched")],
    role: "viewer",
  });
  try {
    const initialText = renderedText(harness.renderer.toJSON());
    assert.match(initialText, /Только операции этого импорта/);
    assert.match(initialText, /Снять ограничение импорта/);
    assert.equal(harness.createCalls.length, 0, "scope initialization must not create a preview");
    assert.equal(
      harness.getCalls.some((path) => path.startsWith("/api/v1/imports")),
      false,
      "review initialization must not probe the import endpoint",
    );

    const accountSelect = harness.root.findAllByType("select")[0];
    await act(async () => accountSelect.props.onChange({ target: { value: account.id } }));
    await submitFilters(harness);

    assert.equal(harness.createCalls.length, 1);
    assert.deepEqual(harness.createCalls[0].body, {
      selection: {
        account_id: account.id,
        import_batch_id: scopedBatchId,
        mode: "filter",
      },
    });
    assert.match(renderedText(harness.renderer.toJSON()), /У вас доступ только на чтение/);
    assert.equal(findButton(harness.root, "Применить выбранные"), undefined);
  } finally {
    await harness.cleanup();
  }
});

test("invalid or duplicate import query fails closed with an explicit broad-review link", async () => {
  const harness = await createHarness({
    importScope: { kind: "invalid", reason: "duplicate" },
  });
  try {
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Ограничение по импорту задано неверно/);
    assert.match(text, /Открыть общую проверку/);
    assert.equal(harness.root.findAllByType("form").length, 0);
    assert.equal(harness.createCalls.length, 0);
    const link = harness.root.findAllByType("a").find((node) => node.props.href === "/rules/review");
    assert.ok(link);
  } finally {
    await harness.cleanup();
  }
});

test("scoped preview overflow keeps the scope and suggests narrowing", async () => {
  const harness = await createHarness({
    importScope: { kind: "valid", importBatchId: scopedBatchId },
    onCreate: () =>
      new ApiClientError(
        "Too many candidate transactions",
        "CATEGORIZATION_PREVIEW_LIMIT_EXCEEDED",
        422,
      ),
  });
  try {
    await submitFilters(harness);
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Слишком много операций/);
    assert.match(text, /Сузьте период или другие фильтры/);
    assert.match(text, /Только операции этого импорта/);
    assert.ok(harness.root.findByType("form"));
    assert.equal(
      (harness.createCalls[0].body as { selection: { import_batch_id: string } }).selection
        .import_batch_id,
      scopedBatchId,
    );
  } finally {
    await harness.cleanup();
  }
});

// 1
test("viewer can build a preview but has no apply control", async () => {
  const harness = await createHarness({ items: [item("a", "matched")], role: "viewer" });
  try {
    await submitFilters(harness);
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /У вас доступ только на чтение/);
    assert.equal(findButton(harness.root, "Применить выбранные"), undefined);
    assert.match(text, /предложено из/);
  } finally { await harness.cleanup(); }
});

// 2 + 3
test("editor creates a preview and sees summary counts", async () => {
  const harness = await createHarness({ items: [item("a", "matched"), item("b", "no_match")] });
  try {
    await submitFilters(harness);
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /2[\s\S]*предложено из[\s\S]*4/);
    assert.match(text, /Нет правила/);
    assert.match(text, /Уже категоризованы/);
    assert.match(text, /Ничего ещё не изменено/);
  } finally { await harness.cleanup(); }
});

// 4 + 19
test("only matched rows expose a checkbox", async () => {
  const harness = await createHarness({ items: [item("a", "matched"), item("b", "no_match"), item("c", "transfer")] });
  try {
    await submitFilters(harness);
    assert.equal(checkboxes(harness.root).length, 1);
  } finally { await harness.cleanup(); }
});

// 5 + 8
test("apply sends only explicitly selected ids with an idempotency key", async () => {
  const harness = await createHarness({ items: [item("a", "matched"), item("b", "matched")] });
  try {
    await submitFilters(harness);
    await toggleRow(harness, 0);
    await act(async () => { button(harness.root, "Применить выбранные").props.onClick(); });
    await act(async () => { button(harness.root, "Подтвердить и применить").props.onClick(); });
    await act(async () => { await settle(); });
    assert.equal(harness.applyCalls.length, 1);
    assert.deepEqual((harness.applyCalls[0].body as { item_ids: string[] }).item_ids, ["a"]);
    const key = (harness.applyCalls[0].headers as Record<string, string>)["X-Idempotency-Key"];
    assert.match(key, /.+/);
  } finally { await harness.cleanup(); }
});

// 6
test("apply is disabled with an empty selection", async () => {
  const harness = await createHarness({ items: [item("a", "matched")] });
  try {
    await submitFilters(harness);
    assert.equal(button(harness.root, "Применить выбранные").props.disabled, true);
  } finally { await harness.cleanup(); }
});

// 7
test("selection cannot exceed the backend maximum of 100", async () => {
  const many = Array.from({ length: 120 }, (_, index) => item(`item-${index}`, "matched"));
  const harness = await createHarness({ items: many });
  try {
    await submitFilters(harness);
    await act(async () => { button(harness.root, "Выбрать предложения на странице").props.onClick(); });
    await act(async () => { await settle(); });
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Выбрано 100 из не более 100/);
    assert.match(text, /не более 100 предложений/);
    await act(async () => { button(harness.root, "Применить выбранные").props.onClick(); });
    await act(async () => { button(harness.root, "Подтвердить и применить").props.onClick(); });
    await act(async () => { await settle(); });
    assert.equal((harness.applyCalls[0].body as { item_ids: string[] }).item_ids.length, 100);
  } finally { await harness.cleanup(); }
});

// 9
test("ambiguous failure offers a safe retry that reuses the same key", async () => {
  let attempt = 0;
  const harness = await createHarness({
    items: [item("a", "matched")],
    onApply: () => {
      attempt += 1;
      if (attempt === 1) return new ApiClientError("Сеть недоступна", "API_NETWORK_ERROR", 0);
      return applyResponse(["applied"], ["a"]);
    },
  });
  try {
    await submitFilters(harness);
    await toggleRow(harness, 0);
    await act(async () => { button(harness.root, "Применить выбранные").props.onClick(); });
    await act(async () => { button(harness.root, "Подтвердить и применить").props.onClick(); });
    await act(async () => { await settle(); });

    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /результат неизвестен/);
    assert.doesNotMatch(text, /ничего не применено/i);

    await act(async () => { button(harness.root, "Повторить безопасно").props.onClick(); });
    await act(async () => { await settle(); });

    assert.equal(harness.applyCalls.length, 2);
    const first = (harness.applyCalls[0].headers as Record<string, string>)["X-Idempotency-Key"];
    const second = (harness.applyCalls[1].headers as Record<string, string>)["X-Idempotency-Key"];
    assert.equal(first, second);
  } finally { await harness.cleanup(); }
});

// 10
test("changing the selection makes the next apply use a new key", async () => {
  const harness = await createHarness({ items: [item("a", "matched"), item("b", "matched")] });
  try {
    await submitFilters(harness);
    await toggleRow(harness, 0);
    await act(async () => { button(harness.root, "Применить выбранные").props.onClick(); });
    await act(async () => { button(harness.root, "Подтвердить и применить").props.onClick(); });
    await act(async () => { await settle(); });

    await act(async () => { button(harness.root, "Вернуться к оставшимся предложениям").props.onClick(); });
    await act(async () => { await settle(); });
    await toggleRow(harness, 0);
    await act(async () => { button(harness.root, "Применить выбранные").props.onClick(); });
    await act(async () => { button(harness.root, "Подтвердить и применить").props.onClick(); });
    await act(async () => { await settle(); });

    assert.equal(harness.applyCalls.length, 2);
    const first = (harness.applyCalls[0].headers as Record<string, string>)["X-Idempotency-Key"];
    const second = (harness.applyCalls[1].headers as Record<string, string>)["X-Idempotency-Key"];
    assert.notEqual(first, second);
  } finally { await harness.cleanup(); }
});

// 11
test("a reordered identical set keeps the pending key", () => {
  const first = nextApplyAttempt(null, "preview-1", ["b", "a"]);
  const second = nextApplyAttempt({ ...first, state: "ambiguous" }, "preview-1", ["a", "b"]);
  assert.equal(second.idempotencyKey, first.idempotencyKey);
  assert.ok(sameItemSet(["b", "a"], ["a", "b"]));
  assert.deepEqual(canonicalItemIds(["b", "a", "b"]), ["a", "b"]);

  const changed = nextApplyAttempt({ ...first, state: "ambiguous" }, "preview-1", ["a", "c"]);
  assert.notEqual(changed.idempotencyKey, first.idempotencyKey);
});

// 12 + 13
test("mixed statuses render and conflicts ask for a new preview", async () => {
  const harness = await createHarness({
    items: [item("a", "matched"), item("b", "matched"), item("c", "matched")],
    onApply: () => applyResponse(["applied", "rule_changed", "closed_period"], ["a", "b", "c"]),
  });
  try {
    await submitFilters(harness);
    await act(async () => { button(harness.root, "Выбрать предложения на странице").props.onClick(); });
    await act(async () => { await settle(); });
    await act(async () => { button(harness.root, "Применить выбранные").props.onClick(); });
    await act(async () => { button(harness.root, "Подтвердить и применить").props.onClick(); });
    await act(async () => { await settle(); });

    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Применено/);
    assert.match(text, /Набор правил изменился/);
    assert.match(text, /Период закрыт/);
    assert.match(text, /Составьте новый список/);
    assert.doesNotMatch(text, /CATEGORIZATION_/);
  } finally { await harness.cleanup(); }
});

// 14
test("an expired preview offers creating a new one", async () => {
  const harness = await createHarness({
    items: [item("a", "matched")],
    onApply: () => new ApiClientError("Список устарел", "CATEGORIZATION_PREVIEW_EXPIRED", 410),
  });
  try {
    await submitFilters(harness);
    await toggleRow(harness, 0);
    await act(async () => { button(harness.root, "Применить выбранные").props.onClick(); });
    await act(async () => { button(harness.root, "Подтвердить и применить").props.onClick(); });
    await act(async () => { await settle(); });
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Список устарел/);
    assert.ok(findButton(harness.root, "Составить новый список"));
  } finally { await harness.cleanup(); }
});

// 15
test("request-level errors render safely", async () => {
  const harness = await createHarness({
    onCreate: () => new ApiClientError("Диапазон дат указан неверно", "VALIDATION_ERROR", 422),
  });
  try {
    await submitFilters(harness);
    const text = renderedText(harness.renderer.toJSON());
    assert.match(text, /Диапазон дат указан неверно/);
    assert.match(text, /VALIDATION_ERROR/);
  } finally { await harness.cleanup(); }
});

// 16
test("a late response from an older preview cannot overwrite newer state", async () => {
  const restoreBrowser = installBrowserGlobals();
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  let resolveOldItems: ((value: CategorizationPreviewItemPage) => void) | undefined;
  let createCall = 0;

  apiClient.get = (<T,>(path: string) => {
    if (path.startsWith("/api/v1/accounts")) return Promise.resolve(page([account]) as T);
    if (path.startsWith("/api/v1/payees")) return Promise.resolve(page([payee]) as T);
    if (path.includes("preview-A")) {
      // Preview A's page stays in flight until the test releases it.
      return new Promise<T>((resolve) => { resolveOldItems = resolve as (value: CategorizationPreviewItemPage) => void; });
    }
    return Promise.resolve({ items: [item("new", "matched")], page: { limit: 50, offset: 0, total: 1 } } as T);
  }) as typeof apiClient.get;

  apiClient.post = (<T,>() => {
    createCall += 1;
    const id = createCall === 1 ? "preview-A" : "preview-B";
    const matched = createCall === 1 ? 1 : 9;
    return Promise.resolve(header({ id, summary: { ...header().summary, matched } }) as T);
  }) as typeof apiClient.post;

  let renderer: ReactTestRenderer | undefined;
  try {
    await act(async () => { renderer = create(<CategorizationReviewScreen onError={() => {}} role="editor" roleLoading={false} />); });
    await act(async () => { await settle(); });
    const root = (renderer as ReactTestRenderer).root;

    // Preview A: header lands, its item page is still open.
    await act(async () => { root.findByType("form").props.onSubmit({ preventDefault() {} }); });
    await act(async () => { await settle(); });

    // The user supersedes it with preview B, which completes.
    await act(async () => { button(root, "Составить другой список").props.onClick(); });
    await act(async () => { await settle(); });
    await act(async () => { root.findByType("form").props.onSubmit({ preventDefault() {} }); });
    await act(async () => { await settle(); });
    assert.match(renderedText((renderer as ReactTestRenderer).toJSON()), /9[\s\S]*предложено из/);

    // Preview A finally answers, far too late.
    await act(async () => { resolveOldItems?.({ items: [item("old", "matched")], page: { limit: 50, offset: 0, total: 1 } }); });
    await act(async () => { await settle(); });

    const text = renderedText((renderer as ReactTestRenderer).toJSON());
    assert.match(text, /9[\s\S]*предложено из/, "новое состояние должно уцелеть");
    assert.doesNotMatch(text, /Контрагент old/, "поздний ответ старого списка не должен подменить строки");
    assert.match(text, /Контрагент new/);
  } finally {
    await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
    apiClient.post = originalPost;
    restoreBrowser();
  }
});

// 17
test("pagination requests stable deterministic offsets", async () => {
  const first = { items: [item("a", "matched")], page: { limit: 50, offset: 0, total: 60 } };
  const second = { items: [item("b", "matched")], page: { limit: 50, offset: 50, total: 60 } };
  const harness = await createHarness({ itemsPages: [first, second, second] });
  try {
    await submitFilters(harness);
    await act(async () => { button(harness.root, "Вперёд").props.onClick(); });
    await act(async () => { await settle(); });
    assert.ok(harness.itemRequests[0].includes("limit=50&offset=0"));
    assert.ok(harness.itemRequests[1].includes("limit=50&offset=50"));
    assert.match(renderedText(harness.renderer.toJSON()), /51–51 из 60/);
  } finally { await harness.cleanup(); }
});

// 18
test("a successful apply does not regenerate the preview", async () => {
  const harness = await createHarness({ items: [item("a", "matched")] });
  try {
    await submitFilters(harness);
    const createCallsBefore = harness.applyCalls.length;
    await toggleRow(harness, 0);
    await act(async () => { button(harness.root, "Применить выбранные").props.onClick(); });
    await act(async () => { button(harness.root, "Подтвердить и применить").props.onClick(); });
    await act(async () => { await settle(); });
    const requestsAfter = harness.itemRequests.length;
    await act(async () => { await settle(); });
    assert.equal(harness.itemRequests.length, requestsAfter, "список не должен перезагружаться сам");
    assert.equal(harness.applyCalls.length, createCallsBefore + 1);
    assert.match(renderedText(harness.renderer.toJSON()), /Результаты/);
    assert.ok(findButton(harness.root, "Составить новый список"));
  } finally { await harness.cleanup(); }
});
