import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { apiClient } from "@/lib/api-client";
import type { Transaction } from "@/types/finance";

import { TransactionsScreen } from "./transactions-screen";

/**
 * Focus regression coverage for the transaction drawer.
 *
 * The drawer shell is not exported, so these tests drive the real screen: it owns the form state
 * and declares closeDrawer as a function declaration, which is exactly the identity churn that used
 * to restart the drawer's focus effect after every keystroke.
 *
 * document.activeElement is modelled explicitly, because focus assertions are meaningless
 * otherwise, and the drawer's DOM queries are answered in real document order with the close button
 * first, so a selector reaching for "input, select, button" genuinely lands on the header cross.
 */

class FocusTarget {
  constructor(
    readonly label: string,
    private readonly setActiveElement: (element: FocusTarget) => void,
  ) {}

  focus() {
    this.setActiveElement(this);
  }
}

interface DrawerNode {
  querySelector: (selector: string) => FocusTarget | null;
  querySelectorAll: () => FocusTarget[];
}

interface BrowserHarness {
  amountControl: FocusTarget;
  animationFrames: () => number;
  closeButton: FocusTarget;
  descriptionControl: FocusTarget;
  dispatchKey: (key: string, shiftKey?: boolean) => { prevented: boolean };
  drawerNode: DrawerNode;
  restore: () => void;
  submitButton: FocusTarget;
}

function installBrowserGlobals(): BrowserHarness {
  const descriptors = new Map<string, PropertyDescriptor | undefined>();
  for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
    descriptors.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  }

  const documentState: { activeElement: FocusTarget | null } = { activeElement: null };
  const setActiveElement = (element: FocusTarget) => {
    documentState.activeElement = element;
  };
  const closeButton = new FocusTarget("close", setActiveElement);
  const amountControl = new FocusTarget("amount", setActiveElement);
  const descriptionControl = new FocusTarget("description", setActiveElement);
  const submitButton = new FocusTarget("submit", setActiveElement);
  // Document order inside the drawer: the header cross first, then form controls, submit last.
  const focusables = [closeButton, amountControl, descriptionControl, submitButton];
  const listeners = new Set<(event: KeyboardEvent) => void>();
  let animationFrames = 0;

  const drawerNode: DrawerNode = {
    querySelector(selector: string) {
      // The pre-fix selector: querySelector follows document order and returns the header button.
      if (selector === "input, select, button") return closeButton;
      if (selector.includes("input") || selector.includes("select") || selector.includes("textarea")) return amountControl;
      if (selector.includes("button")) return closeButton;
      return null;
    },
    querySelectorAll: () => focusables,
  };

  const browser = {
    addEventListener(type: string, listener: (event: KeyboardEvent) => void) {
      if (type === "keydown") listeners.add(listener);
    },
    cancelAnimationFrame: () => undefined,
    removeEventListener(type: string, listener: (event: KeyboardEvent) => void) {
      if (type === "keydown") listeners.delete(listener);
    },
    requestAnimationFrame(callback: () => void) {
      animationFrames += 1;
      callback();
      return animationFrames;
    },
  };

  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: FocusTarget });
  Object.defineProperty(globalThis, "document", { configurable: true, value: documentState });
  Object.defineProperty(globalThis, "window", { configurable: true, value: browser });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });

  return {
    amountControl,
    animationFrames: () => animationFrames,
    closeButton,
    descriptionControl,
    dispatchKey(key: string, shiftKey = false) {
      let prevented = false;
      const event = {
        key,
        preventDefault: () => {
          prevented = true;
        },
        shiftKey,
      } as KeyboardEvent;
      for (const listener of listeners) listener(event);
      return { prevented };
    },
    drawerNode,
    restore() {
      for (const [key, descriptor] of descriptors) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else Reflect.deleteProperty(globalThis, key);
      }
    },
    submitButton,
  };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

const emptyPage = { items: [], page: { limit: 10, offset: 0, total: 0 } };

async function openDrawer(browser: BrowserHarness): Promise<ReactTestRenderer> {
  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(
      <TransactionsScreen onError={(error) => { throw error; }} openForm role="owner" roleLoading={false}/>,
      { createNodeMock: (node) => (node.props as { className?: string }).className === "transaction-drawer" ? browser.drawerNode : null },
    );
    await settle();
  });
  if (!renderer) throw new Error("Renderer was not created");
  return renderer;
}

function amountInput(renderer: ReactTestRenderer) {
  return renderer.root.find((node) => node.type === "input" && node.props.inputMode === "decimal");
}

function descriptionInput(renderer: ReactTestRenderer) {
  return renderer.root.findByProps({ placeholder: "Назначение операции" });
}

function drawerIsOpen(renderer: ReactTestRenderer): boolean {
  return renderer.root.findAll((node) => node.props.className === "transaction-drawer").length > 0;
}

async function withDrawer(body: (browser: BrowserHarness, renderer: ReactTestRenderer) => Promise<void>) {
  const originalGet = apiClient.get;
  apiClient.get = (<T,>() => Promise.resolve(emptyPage as T)) as typeof apiClient.get;
  const browser = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  try {
    renderer = await openDrawer(browser);
    await body(browser, renderer);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    browser.restore();
    apiClient.get = originalGet;
  }
}

test("opening the drawer focuses the amount field, not the close button", async () => {
  await withDrawer(async (browser) => {
    assert.equal(document.activeElement, browser.amountControl);
    assert.notEqual(document.activeElement, browser.closeButton);
  });
});

test("typing an amount keeps focus in the field and preserves every character", async () => {
  await withDrawer(async (browser, renderer) => {
    browser.amountControl.focus();
    let typed = "";
    for (const character of "12345") {
      typed += character;
      const value = typed;
      await act(async () => amountInput(renderer).props.onChange({ target: { value } }));
      assert.equal(document.activeElement, browser.amountControl, `focus left the amount field after "${value}"`);
      assert.notEqual(document.activeElement, browser.closeButton);
      assert.equal(amountInput(renderer).props.value, value);
    }
    assert.equal(amountInput(renderer).props.value, "12345");
  });
});

test("editing another controlled field does not steal focus either", async () => {
  await withDrawer(async (browser, renderer) => {
    browser.descriptionControl.focus();
    await act(async () => descriptionInput(renderer).props.onChange({ target: { value: "Кофе" } }));

    assert.equal(document.activeElement, browser.descriptionControl);
    assert.notEqual(document.activeElement, browser.closeButton);
    assert.equal(descriptionInput(renderer).props.value, "Кофе");
  });
});

test("form state changes do not re-run the drawer initial focus effect", async () => {
  await withDrawer(async (browser, renderer) => {
    assert.equal(browser.animationFrames(), 1);

    await act(async () => amountInput(renderer).props.onChange({ target: { value: "1" } }));
    await act(async () => amountInput(renderer).props.onChange({ target: { value: "12" } }));
    await act(async () => descriptionInput(renderer).props.onChange({ target: { value: "Кофе" } }));

    assert.equal(browser.animationFrames(), 1, "initial focus ran again after a form state change");
  });
});

test("Escape still closes the drawer after the form has been edited", async () => {
  await withDrawer(async (browser, renderer) => {
    await act(async () => amountInput(renderer).props.onChange({ target: { value: "12345" } }));
    assert.equal(drawerIsOpen(renderer), true);

    await act(async () => { browser.dispatchKey("Escape"); });

    assert.equal(drawerIsOpen(renderer), false);
  });
});

test("Tab wraps from the last control back to the first", async () => {
  await withDrawer(async (browser) => {
    browser.submitButton.focus();
    const forward = browser.dispatchKey("Tab");

    assert.equal(forward.prevented, true);
    assert.equal(document.activeElement, browser.closeButton);
  });
});

test("Shift+Tab wraps from the first control back to the last", async () => {
  await withDrawer(async (browser) => {
    browser.closeButton.focus();
    const backward = browser.dispatchKey("Tab", true);

    assert.equal(backward.prevented, true);
    assert.equal(document.activeElement, browser.submitButton);
  });
});

// --- Transaction list: only supported actions are offered --------------------------------------

const listedTransaction: Transaction = {
  account: { id: "account-1", name: "Основной" },
  amount: "1250.0000",
  category: { id: "category-1", name: "Продукты" },
  comment: null,
  counterparty: "Кофейня",
  created_at: "2026-08-20T09:00:00Z",
  currency: "RUB",
  description: "Завтрак",
  external_id: null,
  id: "transaction-1",
  occurred_at: "2026-08-20T09:00:00Z",
  payee: null,
  related_transaction_id: null,
  source: "manual",
  splits: [],
  status: "confirmed",
  target_account: null,
  transaction_type: "expense",
  updated_at: "2026-08-20T09:00:00Z",
  version: 1,
};

/** Mount the screen with one transaction and the drawer closed, i.e. the ordinary ledger view. */
async function withLedger(body: (renderer: ReactTestRenderer) => Promise<void> | void) {
  const originalGet = apiClient.get;
  apiClient.get = (<T,>(path: string) => {
    if (path.startsWith("/api/v1/transactions")) {
      return Promise.resolve({
        items: [listedTransaction],
        page: { limit: 10, offset: 0, total: 1 },
      } as T);
    }
    return Promise.resolve(emptyPage as T);
  }) as typeof apiClient.get;
  const browser = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  try {
    await act(async () => {
      renderer = create(
        <TransactionsScreen onError={(error) => { throw error; }} role="owner" roleLoading={false}/>,
        { createNodeMock: (node) => (node.props as { className?: string }).className === "transaction-drawer" ? browser.drawerNode : null },
      );
      await settle();
    });
    if (!renderer) throw new Error("Renderer was not created");
    await body(renderer);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    browser.restore();
    apiClient.get = originalGet;
  }
}

function textOf(node: unknown): string {
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "children" in node) {
    return textOf((node as { children: unknown }).children);
  }
  return "";
}

function checkboxes(renderer: ReactTestRenderer) {
  return renderer.root.findAllByType("input").filter((node) => node.props.type === "checkbox");
}

function buttonLabels(renderer: ReactTestRenderer): string[] {
  return renderer.root.findAllByType("button").map((node) => textOf(node.props.children));
}

test("the ledger offers no unsupported bulk-edit affordance", async () => {
  await withLedger(async (renderer) => {
    // The toolbar only ever appeared once a row was selected, so selecting is what proves it gone.
    for (const box of checkboxes(renderer)) {
      await act(async () => box.props.onChange({ target: { checked: true } }));
    }

    const rendered = JSON.stringify(renderer.toJSON());
    assert.doesNotMatch(rendered, /Пакетные изменения требуют API/);
    assert.doesNotMatch(rendered, /Выбрано:/);
    assert.doesNotMatch(rendered, /Снять выбор/);
    assert.doesNotMatch(rendered, /Requires API support/);
    assert.doesNotMatch(rendered, /transaction-bulk/);
  });
});

test("no transaction selection controls are rendered at all", async () => {
  await withLedger((renderer) => {
    // Neither per-row checkboxes nor a select-all checkbox: selection had no supported action.
    assert.equal(checkboxes(renderer).length, 0);
    const rendered = JSON.stringify(renderer.toJSON());
    assert.doesNotMatch(rendered, /Выбрать страницу/);
    assert.doesNotMatch(rendered, /Выбрать Кофейня/);
    assert.doesNotMatch(rendered, /is-selected/);
  });
});

test("every real per-transaction action survives", async () => {
  await withLedger((renderer) => {
    const labels = buttonLabels(renderer);
    // Desktop row and mobile card each offer the same real actions.
    for (const label of ["Правило", "Изменить", "История"]) {
      assert.ok(labels.filter((item) => item === label).length >= 2, `missing action: ${label}`);
    }
    // A confirmed transaction can still be cancelled, and the ledger can still be extended.
    assert.ok(labels.includes("Отменить"));
    assert.ok(labels.some((item) => item.includes("Добавить операцию")));
    assert.ok(labels.includes("Обновить"));
  });
});

test("filters, search and the result line still render", async () => {
  await withLedger((renderer) => {
    const rendered = JSON.stringify(renderer.toJSON());
    assert.match(rendered, /Найдено:/);
    assert.ok(renderer.root.findAllByProps({ "aria-label": "Поиск" }).length > 0);
    for (const label of ["Фильтр по статусу", "Фильтр по счёту", "Фильтр по категории"]) {
      assert.ok(renderer.root.findAllByProps({ "aria-label": label }).length > 0, label);
    }
    assert.ok(buttonLabels(renderer).includes("Найти"));
  });
});

test("the mobile card list still renders its transaction without selection controls", async () => {
  await withLedger((renderer) => {
    const cards = renderer.root.findAll(
      (node) => typeof node.props.className === "string"
        && node.props.className.startsWith("transaction-mobile-card"),
    );
    assert.equal(cards.length, 1);
    // Inspect inside the card itself, not the whole screen.
    assert.equal(
      cards[0].findAllByType("input").filter((node) => node.props.type === "checkbox").length,
      0,
    );
    assert.ok(cards[0].findAllByType("button").length >= 3, "card actions disappeared");
  });
});
