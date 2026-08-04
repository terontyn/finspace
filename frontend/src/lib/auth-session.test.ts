import assert from "node:assert/strict";
import test from "node:test";

import type { AuthSession } from "./api-client.ts";
import { restoreAuthState } from "./auth-session.ts";

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
    name: "Personal",
    base_currency: "RUB",
    timezone: "Europe/Amsterdam",
    owner_user_id: "11111111-1111-4111-8111-111111111111",
    version: 1,
  },
};

test("missing session completes loading and redirects to login", async () => {
  const sessions: Array<AuthSession | null> = [];
  const loading: boolean[] = [];
  let redirects = 0;

  await restoreAuthState({
    restoreSession: async () => null,
    isMounted: () => true,
    setSession: (session) => sessions.push(session),
    setLoading: (value) => loading.push(value),
    redirectToLogin: () => {
      redirects += 1;
    },
  });

  assert.deepEqual(sessions, [null]);
  assert.deepEqual(loading, [false]);
  assert.equal(redirects, 1);
});

test("successful restoration completes loading without redirect", async () => {
  const sessions: Array<AuthSession | null> = [];
  const loading: boolean[] = [];
  let redirects = 0;

  await restoreAuthState({
    restoreSession: async () => restoredSession,
    isMounted: () => true,
    setSession: (session) => sessions.push(session),
    setLoading: (value) => loading.push(value),
    redirectToLogin: () => {
      redirects += 1;
    },
  });

  assert.deepEqual(sessions, [restoredSession]);
  assert.deepEqual(loading, [false]);
  assert.equal(redirects, 0);
});

test("rejected restoration logs safely, clears session and always completes loading", async () => {
  const warnings: Array<[string, Record<string, unknown>]> = [];
  const sessions: Array<AuthSession | null> = [];
  const loading: boolean[] = [];
  let redirects = 0;

  await restoreAuthState({
    restoreSession: async () => {
      throw new Error("secret-token-must-not-be-logged");
    },
    isMounted: () => true,
    setSession: (session) => sessions.push(session),
    setLoading: (value) => loading.push(value),
    redirectToLogin: () => {
      redirects += 1;
    },
    logger: { warn: (message, context) => warnings.push([message, context]) },
  });

  assert.deepEqual(sessions, [null]);
  assert.deepEqual(loading, [false]);
  assert.equal(redirects, 1);
  assert.equal(warnings.length, 1);
  assert.doesNotMatch(JSON.stringify(warnings), /secret-token-must-not-be-logged/);
});

test("settled restoration does not update state or redirect after unmount", async () => {
  let mounted = true;
  let resolveRestore: (session: AuthSession | null) => void = () => undefined;
  const pendingRestore = new Promise<AuthSession | null>((resolve) => {
    resolveRestore = resolve;
  });
  const stateUpdates: string[] = [];

  const task = restoreAuthState({
    restoreSession: () => pendingRestore,
    isMounted: () => mounted,
    setSession: () => stateUpdates.push("session"),
    setLoading: () => stateUpdates.push("loading"),
    redirectToLogin: () => stateUpdates.push("redirect"),
  });
  mounted = false;
  resolveRestore(restoredSession);
  await task;

  assert.deepEqual(stateUpdates, []);
});
