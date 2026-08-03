import assert from "node:assert/strict";
import test from "node:test";

import { ApiClient, ApiClientError } from "./api-client.ts";

const authResponse = {
  access_token: "memory-only-access-token",
  token_type: "bearer",
  expires_in: 900,
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

test("login keeps access token in memory and adds bearer/workspace headers", async () => {
  const captured: Headers[] = [];
  const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
    captured.push(new Headers(init?.headers));
    const path = String(input);
    return new Response(JSON.stringify(path.endsWith("/auth/login") ? authResponse : { ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  const client = new ApiClient(fetcher);

  await client.login("person@example.com", "long-password");
  assert.deepEqual(await client.get("/api/v1/example"), { ok: true });
  assert.equal(client.getSession()?.accessToken, authResponse.access_token);
  assert.equal(captured[1].get("Authorization"), `Bearer ${authResponse.access_token}`);
  assert.equal(captured[1].get("X-Workspace-ID"), authResponse.workspace.id);
  assert.equal(captured[1].get("X-User-ID"), null);
});

test("one 401 performs one refresh and retries the original request", async () => {
  let protectedCalls = 0;
  let refreshCalls = 0;
  const fetcher = (async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/auth/refresh")) {
      refreshCalls += 1;
      return new Response(JSON.stringify(authResponse), { status: 200 });
    }
    protectedCalls += 1;
    return protectedCalls === 1
      ? new Response(JSON.stringify({ error: { code: "SESSION_EXPIRED" } }), { status: 401 })
      : new Response(JSON.stringify({ ok: true }), { status: 200 });
  }) as typeof fetch;
  const client = new ApiClient(fetcher);

  assert.deepEqual(await client.get("/api/v1/accounts"), { ok: true });
  assert.equal(refreshCalls, 1);
  assert.equal(protectedCalls, 2);
});

test("failed refresh clears session without an infinite retry", async () => {
  let calls = 0;
  let expired = 0;
  const fetcher = (async () => {
    calls += 1;
    return new Response(JSON.stringify({ error: { code: "SESSION_EXPIRED" } }), { status: 401 });
  }) as typeof fetch;
  const client = new ApiClient(fetcher);
  client.setSessionExpiredHandler(() => { expired += 1; });

  await assert.rejects(client.get("/api/v1/accounts"), ApiClientError);
  assert.equal(calls, 2);
  assert.equal(expired, 1);
  assert.equal(client.getSession(), null);
});

test("API client converts the common error envelope", async () => {
  const fetcher = (async () =>
    new Response(JSON.stringify({ error: { code: "VERSION_CONFLICT", message: "Entity was changed", details: { current_version: 3 }, request_id: "33333333-3333-4333-8333-333333333333" } }), { status: 409 })) as typeof fetch;
  const client = new ApiClient(fetcher);

  await assert.rejects(client.request("/api/v1/auth/login", {}, false), (error: unknown) => {
    assert.ok(error instanceof ApiClientError);
    assert.equal(error.code, "VERSION_CONFLICT");
    assert.deepEqual(error.details, { current_version: 3 });
    return true;
  });
});

test("API client reports an unavailable backend", async () => {
  const fetcher = (async () => { throw new TypeError("connection refused"); }) as typeof fetch;
  await assert.rejects(new ApiClient(fetcher).request("/api/v1/auth/login", {}, false), { code: "API_UNAVAILABLE", status: 0 });
});
