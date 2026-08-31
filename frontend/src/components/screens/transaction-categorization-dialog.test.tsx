import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { ApiClientError, apiClient } from "@/lib/api-client";
import type { CategorizationApplyResult, CategorizationPreview, CategorizationRule } from "@/types/categorization";
import type { Transaction } from "@/types/finance";

import { TransactionCategorizationDialog } from "./transaction-categorization-dialog";

function transaction(overrides: Partial<Transaction> = {}): Transaction {
  return { account: { id: "account-1", name: "Основной" }, amount: "250.0000", category: null, comment: null, counterparty: "COFFEE SHOP 42", created_at: "2026-08-01T00:00:00Z", currency: "RUB", description: "Утренний кофе", external_id: null, id: "transaction-1", occurred_at: "2026-08-01T09:00:00Z", payee: { id: "payee-1", name: "Кофейня" }, related_transaction_id: null, source: "manual", splits: [], status: "confirmed", target_account: null, transaction_type: "expense", updated_at: "2026-08-01T00:00:00Z", version: 7, ...overrides };
}

function rule(): CategorizationRule {
  return { account_id: null, category_id: "category-1", counterparty_contains: "coffee", created_at: "2026-08-01T00:00:00Z", deleted_at: null, description_contains: null, id: "rule-1", is_active: true, name: "Кофе", payee_id: null, priority: 10, transaction_type: "expense", updated_at: "2026-08-01T00:00:00Z", version: 2 };
}

const matchedPreview: CategorizationPreview = { category: { id: "category-1", name: "Кафе" }, matched: true, rule: rule() };

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
  canApply?: boolean;
  current?: Transaction;
  onGet?: (path: string) => unknown | Promise<unknown>;
  onPost?: (path: string, body: unknown) => unknown | Promise<unknown>;
  rerenderOnApplied?: boolean;
}

async function createHarness(options: HarnessOptions = {}) {
  const restoreBrowser = installBrowserGlobals();
  const originalGet = apiClient.get;
  const originalPost = apiClient.post;
  const calls: Array<{ body: unknown; path: string }> = [];
  const gets: string[] = [];
  const applied: Transaction[] = [];
  const errors: unknown[] = [];
  let renderer: ReactTestRenderer | undefined;
  let current = options.current ?? transaction();
  apiClient.get = (<T,>(path: string) => { gets.push(path); if (!options.onGet) throw new Error(`Unexpected GET ${path}`); return Promise.resolve(options.onGet(path)) as Promise<T>; }) as typeof apiClient.get;
  apiClient.post = (<T,>(path: string, body?: unknown) => { calls.push({ body, path }); return Promise.resolve(options.onPost?.(path, body) ?? matchedPreview) as Promise<T>; }) as typeof apiClient.post;
  // The real parent screen stores the transaction in state and replaces it whenever the dialog
  // reports a fresher record, so opting in mirrors production prop flow.
  const element = (value: Transaction) => <TransactionCategorizationDialog canApply={options.canApply ?? true} onApplied={(updated) => { applied.push(updated); current = updated; if (options.rerenderOnApplied) renderer?.update(element(updated)); }} onClose={() => undefined} onError={(error) => errors.push(error)} roleLoading={false} transaction={value}/>;
  await act(async () => { renderer = create(element(current)); await settle(); });
  if (!renderer) throw new Error("Renderer was not created");
  return { applied, calls, errors, gets, renderer, async cleanup() { await act(async () => renderer?.unmount()); apiClient.get = originalGet; apiClient.post = originalPost; restoreBrowser(); } };
}

test("matched preview is non-mutating and apply is a separate versioned command", async () => {
  const updated = transaction({ category: { id: "category-1", name: "Кафе" }, version: 8 });
  const applyResult: CategorizationApplyResult = { applied: true, category: { id: "category-1", name: "Кафе" }, reason: "applied", rule: rule(), transaction: updated };
  const harness = await createHarness({ onPost: (path) => path.endsWith("/preview") ? matchedPreview : applyResult });
  try {
    const initialText = renderedText(harness.renderer.toJSON());
    assert.match(initialText, /ПолучательКофейня/);
    assert.match(initialText, /Исходный контрагентCOFFEE SHOP 42/);
    assert.match(initialText, /проверяются независимо/);

    await act(async () => { button(harness.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    assert.deepEqual(harness.calls, [{ body: { transaction_id: "transaction-1" }, path: "/api/v1/categorization-rules/preview" }]);
    assert.equal(harness.applied.length, 0);
    assert.match(renderedText(harness.renderer.toJSON()), /Совпадение найдено/);
    assert.match(renderedText(harness.renderer.toJSON()), /ПравилоКофе/);
    assert.match(renderedText(harness.renderer.toJSON()), /Целевая категорияКафе/);

    await act(async () => { button(harness.renderer.root, "Применить категорию").props.onClick(); await settle(); });
    assert.deepEqual(harness.calls[1], { body: { version: 7 }, path: "/api/v1/transactions/transaction-1/apply-categorization" });
    assert.deepEqual(harness.applied, [updated]);
    assert.match(renderedText(harness.renderer.toJSON()), /Операция обновлена до версии 8/);
  } finally { await harness.cleanup(); }
});

test("preview no-match remains read-only and viewer cannot apply a matched result", async () => {
  const noMatch = await createHarness({ onPost: () => ({ category: null, matched: false, rule: null }) });
  try {
    await act(async () => { button(noMatch.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    assert.match(renderedText(noMatch.renderer.toJSON()), /Подходящего правила нет/);
    assert.equal(noMatch.renderer.root.findAllByType("button").some((node) => renderedText(node.props.children).includes("Применить категорию")), false);
  } finally { await noMatch.cleanup(); }

  const viewer = await createHarness({ canApply: false });
  try {
    await act(async () => { button(viewer.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    assert.match(renderedText(viewer.renderer.toJSON()), /Режим просмотра/);
    assert.equal(viewer.renderer.root.findAllByType("button").some((node) => renderedText(node.props.children).includes("Применить категорию")), false);
  } finally { await viewer.cleanup(); }
});

test("rule-change and stale-version errors invalidate preview without a silent retry", async () => {
  for (const code of ["CATEGORIZATION_RULE_CHANGED", "VERSION_CONFLICT"] as const) {
    let request = 0;
    const harness = await createHarness({ onPost: (path) => {
      request += 1;
      if (path.endsWith("/preview")) return matchedPreview;
      throw new ApiClientError("conflict", code, 409, "request-1");
    } });
    try {
      await act(async () => { button(harness.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
      await act(async () => { button(harness.renderer.root, "Применить категорию").props.onClick(); await settle(); });
      const text = renderedText(harness.renderer.toJSON());
      assert.match(text, new RegExp(code));
      assert.equal(request, 2);
      assert.equal(harness.calls.length, 2);
      assert.equal(harness.renderer.root.findAllByType("button").some((node) => renderedText(node.props.children).includes("Применить категорию")), false);
      assert.match(text, code === "CATEGORIZATION_RULE_CHANGED" ? /Выполните предпросмотр ещё раз/ : /актуальной версией/);
    } finally { await harness.cleanup(); }
  }
});

test("categorized, split and transfer transactions never expose preview or overwrite actions", async () => {
  const variants = [
    transaction({ category: { id: "category-1", name: "Кафе" } }),
    transaction({ splits: [{ amount: "250.0000", category_id: "category-1", category_name: "Кафе", id: "split-1" }] }),
    transaction({ transaction_type: "transfer" }),
  ];
  for (const current of variants) {
    const harness = await createHarness({ current });
    try {
      const text = renderedText(harness.renderer.toJSON());
      assert.match(text, current.transaction_type === "transfer" ? /Переводы не категоризируются/ : /Категоризация уже задана/);
      assert.equal(harness.renderer.root.findAllByType("button").some((node) => renderedText(node.props.children).includes("Предпросмотр")), false);
      assert.equal(harness.renderer.root.findAllByType("button").some((node) => renderedText(node.props.children).includes("Применить категорию")), false);
      assert.equal(harness.calls.length, 0);
    } finally { await harness.cleanup(); }
  }
});

test("stale-version apply re-reads the transaction so a repeat preview cannot reuse the old version", async () => {
  const refreshed = transaction({ version: 9 });
  let applyAttempts = 0;
  const harness = await createHarness({
    onGet: () => refreshed,
    onPost: (path, body) => {
      if (path.endsWith("/preview")) return matchedPreview;
      applyAttempts += 1;
      if ((body as { version: number }).version !== 9) throw new ApiClientError("conflict", "VERSION_CONFLICT", 409, "request-1");
      return { applied: true, category: { id: "category-1", name: "Кафе" }, reason: "applied", rule: rule(), transaction: transaction({ category: { id: "category-1", name: "Кафе" }, version: 10 }) } satisfies CategorizationApplyResult;
    },
    rerenderOnApplied: true,
  });
  try {
    await act(async () => { button(harness.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    await act(async () => { button(harness.renderer.root, "Применить категорию").props.onClick(); await settle(); });

    assert.deepEqual(harness.calls[1], { body: { version: 7 }, path: "/api/v1/transactions/transaction-1/apply-categorization" });
    assert.deepEqual(harness.gets, ["/api/v1/transactions/transaction-1"]);
    assert.deepEqual(harness.applied, [refreshed]);
    const conflictText = renderedText(harness.renderer.toJSON());
    assert.match(conflictText, /VERSION_CONFLICT/);
    assert.match(conflictText, /Версияv9/);
    assert.equal(harness.renderer.root.findAllByType("button").some((node) => renderedText(node.props.children).includes("Применить категорию")), false);

    await act(async () => { button(harness.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    await act(async () => { button(harness.renderer.root, "Применить категорию").props.onClick(); await settle(); });
    assert.deepEqual(harness.calls.at(-1), { body: { version: 9 }, path: "/api/v1/transactions/transaction-1/apply-categorization" });
    assert.equal(applyAttempts, 2);
    assert.equal(harness.applied.at(-1)?.version, 10);
    const appliedText = renderedText(harness.renderer.toJSON());
    assert.match(appliedText, /Версияv10/);
    assert.doesNotMatch(appliedText, /VERSION_CONFLICT/);
  } finally { await harness.cleanup(); }
});

test("apply stays blocked when the stale transaction cannot be re-read", async () => {
  const harness = await createHarness({
    onGet: () => { throw new ApiClientError("gone", "TRANSACTION_NOT_FOUND", 404, "request-2"); },
    onPost: (path) => { if (path.endsWith("/preview")) return matchedPreview; throw new ApiClientError("conflict", "VERSION_CONFLICT", 409, "request-1"); },
    rerenderOnApplied: true,
  });
  try {
    await act(async () => { button(harness.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    await act(async () => { button(harness.renderer.root, "Применить категорию").props.onClick(); await settle(); });
    assert.equal(harness.applied.length, 0);
    assert.match(renderedText(harness.renderer.toJSON()), /Версия операции устарела/);

    await act(async () => { button(harness.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /Совпадение найдено/);
    assert.equal(harness.renderer.root.findAllByType("button").some((node) => renderedText(node.props.children).includes("Применить категорию")), false);
    assert.equal(harness.calls.filter((call) => call.path.endsWith("/apply-categorization")).length, 1);
  } finally { await harness.cleanup(); }
});

test("rule-change conflict keeps the transaction untouched and allows a fresh preview", async () => {
  let applyCalls = 0;
  const harness = await createHarness({
    onGet: () => { throw new Error("VERSION_CONFLICT recovery must not run for rule changes"); },
    onPost: (path) => { if (path.endsWith("/preview")) return matchedPreview; applyCalls += 1; throw new ApiClientError("conflict", "CATEGORIZATION_RULE_CHANGED", 409, "request-3"); },
    rerenderOnApplied: true,
  });
  try {
    await act(async () => { button(harness.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    await act(async () => { button(harness.renderer.root, "Применить категорию").props.onClick(); await settle(); });
    assert.equal(harness.gets.length, 0);
    assert.equal(harness.applied.length, 0);
    assert.match(renderedText(harness.renderer.toJSON()), /Выполните предпросмотр ещё раз/);

    await act(async () => { button(harness.renderer.root, "Предпросмотр").props.onClick(); await settle(); });
    assert.match(renderedText(harness.renderer.toJSON()), /Версияv7/);
    assert.equal(harness.renderer.root.findAllByType("button").some((node) => renderedText(node.props.children).includes("Применить категорию")), true);
    await act(async () => { button(harness.renderer.root, "Применить категорию").props.onClick(); await settle(); });
    assert.equal(applyCalls, 2);
  } finally { await harness.cleanup(); }
});
