import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { AuthProvider } from "./auth-provider";
import { FinanceApp } from "./finance-app";
import { apiClient, type AuthSession } from "../lib/api-client";

const restoredSession: AuthSession = {
  accessToken: "memory-only-access-token",
  expiresIn: 900,
  user: {
    id: "11111111-1111-4111-8111-111111111111",
    email: "person@example.com",
    display_name: "Person",
    locale: "ru-RU",
    timezone: "Europe/Amsterdam",
    is_active: true,
    version: 1,
  },
  workspace: {
    id: "22222222-2222-4222-8222-222222222222",
    name: "Personal workspace",
    base_currency: "RUB",
    timezone: "Europe/Amsterdam",
    owner_user_id: "11111111-1111-4111-8111-111111111111",
    version: 1,
  },
};

function installTestWindow(): () => void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: {
        pathname: "/",
        replace: () => undefined,
      },
    },
  });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", {
    configurable: true,
    value: true,
  });
  return () => {
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
    else Reflect.deleteProperty(globalThis, "window");
    Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
  };
}

function renderFinanceApp() {
  return create(
    <AuthProvider>
      <FinanceApp />
    </AuthProvider>,
  );
}

test("shows session restoration while auth is loading", async () => {
  const restoreWindow = installTestWindow();
  const originalRestoreSession = apiClient.restoreSession;
  let renderer: ReactTestRenderer | undefined;
  apiClient.restoreSession = () => new Promise<AuthSession | null>(() => undefined);

  try {
    await act(async () => {
      renderer = renderFinanceApp();
    });
    const loadingPage = renderer?.root.findByProps({ className: "loading-page" });
    assert.equal(loadingPage?.children.join(""), "Восстанавливаем защищённую сессию…");
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.restoreSession = originalRestoreSession;
    restoreWindow();
  }
});

test("redirects to login when loading is complete without a session", async () => {
  const restoreWindow = installTestWindow();
  const originalRestoreSession = apiClient.restoreSession;
  let renderer: ReactTestRenderer | undefined;
  apiClient.restoreSession = async () => null;

  try {
    await assert.rejects(
      async () => {
        await act(async () => {
          renderer = renderFinanceApp();
        });
      },
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, "NEXT_REDIRECT");
        assert.match("digest" in error ? String(error.digest) : "", /\/login/);
        return true;
      },
    );
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.restoreSession = originalRestoreSession;
    restoreWindow();
  }
});

test("renders the finance application when a session is restored", async () => {
  const restoreWindow = installTestWindow();
  const originalRestoreSession = apiClient.restoreSession;
  const originalGet = apiClient.get;
  let renderer: ReactTestRenderer | undefined;
  apiClient.restoreSession = async () => restoredSession;
  apiClient.get = (<T,>(path: string) =>
    Promise.resolve(
      (path === "/api/v1/accounts/balances" ? [] : { groups: [] }) as T,
    )) as typeof apiClient.get;

  try {
    await act(async () => {
      renderer = renderFinanceApp();
      await Promise.resolve();
    });
    assert.ok(renderer?.root.findByProps({ className: "app-shell" }));
    assert.equal(
      renderer?.root.findAllByType("span").some((node) =>
        node.children.includes(restoredSession.workspace.name),
      ),
      true,
    );
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.restoreSession = originalRestoreSession;
    apiClient.get = originalGet;
    restoreWindow();
  }
});
