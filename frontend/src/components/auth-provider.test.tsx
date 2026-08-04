import assert from "node:assert/strict";
import test from "node:test";

import { StrictMode } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { AuthProvider, useAuth } from "./auth-provider";
import { ApiClient, apiClient } from "../lib/api-client";

function AuthStateProbe() {
  const auth = useAuth();
  return <span>{auth.loading ? "loading" : "ready"}</span>;
}

test("StrictMode finishes a timed-out restoration and redirects to login", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalRestoreSession = apiClient.restoreSession;
  const redirects: string[] = [];
  let fetchCalls = 0;
  let restoreCalls = 0;
  let renderer: ReactTestRenderer | undefined;

  const hangingFetch = (() => {
    fetchCalls += 1;
    return new Promise<Response>(() => undefined);
  }) as typeof fetch;
  const testClient = new ApiClient(hangingFetch, { warn: () => undefined });

  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: {
        pathname: "/",
        replace: (path: string) => redirects.push(path),
      },
    },
  });
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", {
    configurable: true,
    value: true,
  });
  apiClient.restoreSession = (options) => {
    restoreCalls += 1;
    return testClient.restoreSession(options);
  };

  try {
    await act(async () => {
      renderer = create(
        <StrictMode>
          <AuthProvider>
            <AuthStateProbe />
          </AuthProvider>
        </StrictMode>,
      );
    });

    assert.equal(restoreCalls, 2, "StrictMode must run the effect setup twice");
    assert.equal(fetchCalls, 1, "both effects must share the in-flight restoration");
    assert.equal(renderer?.root.findByType("span").children.join(""), "loading");

    await act(async () => {
      context.mock.timers.tick(10_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    assert.equal(renderer?.root.findByType("span").children.join(""), "ready");
    assert.deepEqual(redirects, ["/login"]);
  } finally {
    if (renderer) {
      await act(async () => renderer?.unmount());
    }
    apiClient.restoreSession = originalRestoreSession;
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
    else Reflect.deleteProperty(globalThis, "window");
    Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
  }
});
