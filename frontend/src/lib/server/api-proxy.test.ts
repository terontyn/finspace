import assert from "node:assert/strict";
import test from "node:test";

import { proxyApiRequest } from "./api-proxy.ts";

test("same-origin proxy forwards method, query, body, cookies and response metadata", async () => {
  let upstreamUrl = "";
  let upstreamInit: RequestInit | undefined;
  const upstreamHeaders = new Headers({
    "Content-Type": "application/json",
    "X-Request-ID": "request-123",
  });
  upstreamHeaders.append("Set-Cookie", "refresh=rotated; Path=/; HttpOnly; Secure; SameSite=Lax");
  upstreamHeaders.append("Set-Cookie", "csrf=value; Path=/; Secure; SameSite=Lax");
  const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
    upstreamUrl = String(input);
    upstreamInit = init;
    return new Response(JSON.stringify({ accepted: true }), {
      status: 202,
      headers: upstreamHeaders,
    });
  }) as typeof fetch;
  const request = new Request("https://finspace.example/api/v1/auth/login?source=browser", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: "refresh=old",
      Host: "finspace.example",
      Connection: "keep-alive",
    },
    body: JSON.stringify({ email: "person@example.com", password: "long-password" }),
  });

  const response = await proxyApiRequest(request, {
    path: ["v1", "auth", "login"],
    fetcher,
    internalApiUrl: "http://backend:8000",
  });

  assert.equal(upstreamUrl, "http://backend:8000/api/v1/auth/login?source=browser");
  assert.equal(upstreamInit?.method, "POST");
  assert.equal(new Headers(upstreamInit?.headers).get("cookie"), "refresh=old");
  assert.equal(new Headers(upstreamInit?.headers).get("host"), null);
  assert.equal(new Headers(upstreamInit?.headers).get("connection"), null);
  assert.deepEqual(JSON.parse(await new Response(upstreamInit?.body).text()), {
    email: "person@example.com",
    password: "long-password",
  });
  assert.equal(response.status, 202);
  assert.equal(response.headers.get("X-Request-ID"), "request-123");
  assert.deepEqual(response.headers.getSetCookie(), [
    "refresh=rotated; Path=/; HttpOnly; Secure; SameSite=Lax",
    "csrf=value; Path=/; Secure; SameSite=Lax",
  ]);
  assert.deepEqual(await response.json(), { accepted: true });
});

test("same-origin proxy preserves a streamed response body", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("first-"));
      controller.enqueue(new TextEncoder().encode("second"));
      controller.close();
    },
  });
  const fetcher = (async () => new Response(stream, { status: 206 })) as typeof fetch;

  const response = await proxyApiRequest(
    new Request("https://finspace.example/api/v1/export"),
    { path: ["v1", "export"], fetcher, internalApiUrl: "http://backend:8000" },
  );

  assert.equal(response.status, 206);
  assert.equal(await response.text(), "first-second");
});

test("same-origin proxy returns a safe 502 when the internal request fails", async () => {
  const secretMarker = "internal-address-or-secret-must-not-leak";
  const warnings: Array<[string, Record<string, unknown>]> = [];
  const fetcher = (async () => { throw new TypeError(secretMarker); }) as typeof fetch;

  const response = await proxyApiRequest(
    new Request("https://finspace.example/api/v1/auth/refresh", { method: "POST" }),
    {
      path: ["v1", "auth", "refresh"],
      fetcher,
      internalApiUrl: "http://backend:8000",
      logger: { warn: (message, context) => warnings.push([message, context]) },
    },
  );

  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), {
    error: { code: "API_PROXY_UNAVAILABLE", message: "API temporarily unavailable." },
  });
  assert.doesNotMatch(JSON.stringify(warnings), new RegExp(secretMarker));
  assert.deepEqual(warnings[0]?.[1], {
    reason: "upstream_fetch_failed",
    method: "POST",
    error_name: "TypeError",
  });
});
