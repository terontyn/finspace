import assert from "node:assert/strict";
import test from "node:test";

import { parseCategorizationReviewImportScope } from "./categorization-review-scope";

test("absent import scope keeps the broad review", () => {
  assert.deepEqual(parseCategorizationReviewImportScope(undefined), { kind: "none" });
});

test("one canonical UUID creates an exact normalized import scope", () => {
  assert.deepEqual(
    parseCategorizationReviewImportScope("152EABF4-4447-4F22-8ADA-95647FF80F30"),
    { kind: "valid", importBatchId: "152eabf4-4447-4f22-8ada-95647ff80f30" },
  );
});

test("malformed and duplicate import scopes fail closed", () => {
  assert.deepEqual(parseCategorizationReviewImportScope("batch-1"), {
    kind: "invalid",
    reason: "malformed",
  });
  assert.deepEqual(
    parseCategorizationReviewImportScope([
      "152eabf4-4447-4f22-8ada-95647ff80f30",
      "9a458b34-fd06-4c9a-b801-790a5c13db52",
    ]),
    { kind: "invalid", reason: "duplicate" },
  );
});
