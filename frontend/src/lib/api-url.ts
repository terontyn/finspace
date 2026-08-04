const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL ?? "/";

export const publicApiUrl = configuredApiUrl;

export function buildApiUrl(baseUrl: string, path: string): string {
  const normalizedPath = `/${path.replace(/^\/+/, "")}`;
  const normalizedBase = baseUrl.trim().replace(/\/+$/, "");

  if (!normalizedBase) return normalizedPath;
  return `${normalizedBase}${normalizedPath}`;
}
