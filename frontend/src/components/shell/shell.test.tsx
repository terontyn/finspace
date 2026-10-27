import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { CommandPalette } from "./command-palette";
import { MobileNav } from "./mobile-nav";
import { navigationItems } from "./navigation";
import { resolveStoredTheme } from "./theme-toggle";

function installWindow(): () => void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  const originalActEnvironment = Object.getOwnPropertyDescriptor(
    globalThis,
    "IS_REACT_ACT_ENVIRONMENT",
  );
  const browserGlobal = {
    addEventListener: () => undefined,
    cancelAnimationFrame: () => undefined,
    cancelIdleCallback: () => undefined,
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    removeEventListener: () => undefined,
    requestAnimationFrame: (callback: () => void) => { callback(); return 1; },
    requestIdleCallback: (callback: () => void) => { callback(); return 1; },
    setTimeout: globalThis.setTimeout.bind(globalThis),
  };
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: browserGlobal,
  });
  Object.defineProperty(globalThis, "self", {
    configurable: true,
    value: browserGlobal,
  });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });
  return () => {
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow); else Reflect.deleteProperty(globalThis, "window");
    if (originalSelf) Object.defineProperty(globalThis, "self", originalSelf); else Reflect.deleteProperty(globalThis, "self");
    if (originalActEnvironment) {
      Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", originalActEnvironment);
    } else {
      Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
    }
  };
}

test("production navigation uses unique deep links", () => {
  const hrefs = navigationItems.map((item) => item.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
  assert.ok(hrefs.includes("/transactions"));
  assert.ok(hrefs.includes("/integrations/google"));
  assert.ok(hrefs.includes("/month-close"));
});

test("mobile navigation exposes the primary production routes", async () => {
  const restore = installWindow();
  let renderer: ReactTestRenderer | undefined;
  try {
    await act(async () => { renderer = create(<MobileNav activeScreen="today" onMore={() => undefined} />); });
    const links = renderer?.root.findAllByType("a") ?? [];
    assert.deepEqual(links.map((link) => link.props.href), ["/", "/transactions", "/accounts", "/categories"]);
    assert.equal(links[0]?.props.className, "is-active");
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    restore();
  }
});

test("command palette renders keyboard-searchable route links", async () => {
  const restore = installWindow(); let renderer: ReactTestRenderer | undefined;
  try {
    await act(async () => { renderer = create(<CommandPalette isOpen onClose={() => undefined} />); });
    const dialog = renderer?.root.findByProps({ role: "dialog" });
    assert.equal(dialog?.props["aria-label"], "Командная палитра");
    assert.equal(renderer?.root.findAllByType("a").length, navigationItems.length);
  } finally { if (renderer) await act(async () => renderer?.unmount()); restore(); }
});

test("theme preference accepts only the supported light value", () => {
  assert.equal(resolveStoredTheme("light"), "light");
  assert.equal(resolveStoredTheme("dark"), "dark");
  assert.equal(resolveStoredTheme("unexpected"), "dark");
  assert.equal(resolveStoredTheme(null), "dark");
});
