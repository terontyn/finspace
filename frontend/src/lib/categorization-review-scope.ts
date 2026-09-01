export type CategorizationReviewImportScope =
  | { kind: "none" }
  | { kind: "valid"; importBatchId: string }
  | { kind: "invalid"; reason: "duplicate" | "malformed" };

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Query parsing is deliberately fail-closed: ambiguous scopes never become a broad review. */
export function parseCategorizationReviewImportScope(
  value: string | string[] | undefined,
): CategorizationReviewImportScope {
  if (value === undefined) return { kind: "none" };
  if (Array.isArray(value)) return { kind: "invalid", reason: "duplicate" };
  if (!CANONICAL_UUID.test(value)) return { kind: "invalid", reason: "malformed" };
  return { kind: "valid", importBatchId: value.toLowerCase() };
}
