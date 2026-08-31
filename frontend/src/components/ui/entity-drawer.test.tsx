import assert from "node:assert/strict";
import test from "node:test";

import { useState, type ReactElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { EntityDrawer } from "./entity-drawer";

class FocusTarget {
  constructor(
    readonly label: string,
    private readonly setActiveElement: (element: FocusTarget) => void,
  ) {}

  focus() {
    this.setActiveElement(this);
  }
}

interface BrowserHarness {
  closeButton: FocusTarget;
  dispatchKey: (key: string, shiftKey?: boolean) => { prevented: boolean };
  input: FocusTarget;
  opener: FocusTarget;
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
  const opener = new FocusTarget("opener", setActiveElement);
  const closeButton = new FocusTarget("close", setActiveElement);
  const input = new FocusTarget("input", setActiveElement);
  const submitButton = new FocusTarget("submit", setActiveElement);
  const listeners = new Set<(event: KeyboardEvent) => void>();
  const browser = {
    addEventListener(type: string, listener: (event: KeyboardEvent) => void) {
      if (type === "keydown") listeners.add(listener);
    },
    cancelAnimationFrame: () => undefined,
    removeEventListener(type: string, listener: (event: KeyboardEvent) => void) {
      if (type === "keydown") listeners.delete(listener);
    },
    requestAnimationFrame(callback: () => void) {
      callback();
      return 1;
    },
  };

  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: FocusTarget });
  Object.defineProperty(globalThis, "document", { configurable: true, value: documentState });
  Object.defineProperty(globalThis, "window", { configurable: true, value: browser });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });

  return {
    closeButton,
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
    input,
    opener,
    restore() {
      for (const [key, descriptor] of descriptors) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else Reflect.deleteProperty(globalThis, key);
      }
    },
    submitButton,
  };
}

function drawer(onClose: () => void, children = <input aria-label="Название" />) {
  return <EntityDrawer ariaLabel="Редактирование" eyebrow="Объект" onClose={onClose} title="Форма">
    {children}
    <button type="button">Сохранить</button>
  </EntityDrawer>;
}

function controlledDrawer(onClose: () => void) {
  function ControlledDrawer() {
    const [value, setValue] = useState("");
    return drawer(
      () => onClose(),
      <input aria-label="Название" onChange={(event) => setValue(event.target.value)} value={value} />,
    );
  }
  return <ControlledDrawer />;
}

async function mount(browser: BrowserHarness, element: ReactElement): Promise<ReactTestRenderer> {
  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(element, {
      createNodeMock: (node) => node.type === "section" ? {
        querySelector: (selector: string) => {
          // Current main asks for this broad selector and receives the header button
          // first because querySelector follows DOM order.
          if (selector === "input, select, button") return browser.closeButton;
          if (selector.includes("input") || selector.includes("select") || selector.includes("textarea")) return browser.input;
          if (selector.includes("button")) return browser.closeButton;
          return null;
        },
        querySelectorAll: () => [browser.closeButton, browser.input, browser.submitButton],
      } : null,
    });
  });
  if (!renderer) throw new Error("Renderer was not created");
  return renderer;
}

test("controlled rerenders do not steal focus when onClose identity changes", async () => {
  const browser = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  try {
    renderer = await mount(browser, controlledDrawer(() => undefined));
    browser.input.focus();

    await act(async () => renderer?.root.findByType("input").props.onChange({ target: { value: "Б" } }));

    assert.equal(document.activeElement, browser.input);
    assert.notEqual(document.activeElement, browser.closeButton);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    browser.restore();
  }
});

test("initial focus prefers a meaningful form control over the header close button", async () => {
  const browser = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  try {
    renderer = await mount(browser, drawer(() => undefined));
    assert.equal(document.activeElement, browser.input);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    browser.restore();
  }
});

test("Escape invokes only the latest onClose callback", async () => {
  const browser = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  let firstCalls = 0;
  let latestCalls = 0;
  try {
    renderer = await mount(browser, drawer(() => { firstCalls += 1; }));
    await act(async () => renderer?.update(drawer(() => { latestCalls += 1; })));

    browser.dispatchKey("Escape");

    assert.equal(firstCalls, 0);
    assert.equal(latestCalls, 1);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    browser.restore();
  }
});

test("Tab and Shift+Tab wrap within the drawer", async () => {
  const browser = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  try {
    renderer = await mount(browser, drawer(() => undefined));

    browser.submitButton.focus();
    const forward = browser.dispatchKey("Tab");
    assert.equal(forward.prevented, true);
    assert.equal(document.activeElement, browser.closeButton);

    browser.closeButton.focus();
    const backward = browser.dispatchKey("Tab", true);
    assert.equal(backward.prevented, true);
    assert.equal(document.activeElement, browser.submitButton);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    browser.restore();
  }
});

test("unmount restores focus to the element that opened the drawer", async () => {
  const browser = installBrowserGlobals();
  let renderer: ReactTestRenderer | undefined;
  try {
    browser.opener.focus();
    renderer = await mount(browser, drawer(() => undefined));
    assert.equal(document.activeElement, browser.input);

    await act(async () => renderer?.unmount());
    renderer = undefined;

    assert.equal(document.activeElement, browser.opener);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    browser.restore();
  }
});
