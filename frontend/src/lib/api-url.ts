const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL ?? "/";

export function normalizeApiBase(value: string): string {
  const normalized = value.trim().replace(/\/+$/, "");
  return normalized === "" ? "" : normalized;
}

export const publicApiUrl = normalizeApiBase(configuredApiUrl);

export function buildApiUrl(path: string, base = publicApiUrl): string {
  if (!path.startsWith("/")) {
    throw new Error("API path must start with a slash");
  }
  return `${normalizeApiBase(base)}${path}`;
}
