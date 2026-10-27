import assert from "node:assert/strict";
import test from "node:test";

import nextConfig from "../../next.config.ts";

test("production rewrite sends same-origin API paths to the internal backend", async () => {
  assert.ok(nextConfig.rewrites);
  const rewrites = await nextConfig.rewrites();
  assert.ok(Array.isArray(rewrites));
  assert.deepEqual(rewrites[0], {
    source: "/api/:path*",
    destination: `${process.env.INTERNAL_API_URL ?? "http://backend:8000"}/api/:path*`,
  });
});
