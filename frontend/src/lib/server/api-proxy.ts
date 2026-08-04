const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "content-length",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

interface ProxyLogger {
  warn(message: string, context: Record<string, unknown>): void;
}

export interface ApiProxyOptions {
  path: string[];
  fetcher?: typeof fetch;
  internalApiUrl?: string;
  logger?: ProxyLogger;
}

function copyRequestHeaders(source: Headers): Headers {
  const headers = new Headers();
  source.forEach((value, name) => {
    if (!HOP_BY_HOP_HEADERS.has(name.toLowerCase()) && name.toLowerCase() !== "host") {
      headers.append(name, value);
    }
  });
  return headers;
}

function copyResponseHeaders(source: Headers): Headers {
  const headers = new Headers();
  source.forEach((value, name) => {
    const lowerName = name.toLowerCase();
    if (!HOP_BY_HOP_HEADERS.has(lowerName) && lowerName !== "set-cookie") {
      headers.append(name, value);
    }
  });

  const setCookies = source.getSetCookie();
  if (setCookies.length > 0) {
    setCookies.forEach((cookie) => headers.append("set-cookie", cookie));
  } else {
    const combinedSetCookie = source.get("set-cookie");
    if (combinedSetCookie) headers.append("set-cookie", combinedSetCookie);
  }
  return headers;
}

function buildInternalUrl(requestUrl: string, path: string[], internalApiUrl: string): URL {
  const base = internalApiUrl.endsWith("/") ? internalApiUrl : `${internalApiUrl}/`;
  const target = new URL(`api/${path.map(encodeURIComponent).join("/")}`, base);
  target.search = new URL(requestUrl).search;
  return target;
}

export async function proxyApiRequest(
  request: Request,
  options: ApiProxyOptions,
): Promise<Response> {
  const fetcher = options.fetcher ?? fetch;
  const logger = options.logger ?? console;
  const internalApiUrl = options.internalApiUrl ?? process.env.INTERNAL_API_URL ?? "http://backend:8000";
  const target = buildInternalUrl(request.url, options.path, internalApiUrl);
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const init: RequestInit & { duplex?: "half" } = {
    method: request.method,
    headers: copyRequestHeaders(request.headers),
    body: hasBody ? request.body : undefined,
    cache: "no-store",
    redirect: "manual",
    signal: request.signal,
  };
  if (hasBody && request.body) init.duplex = "half";

  try {
    const upstream = await fetcher(target, init);
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: copyResponseHeaders(upstream.headers),
    });
  } catch (error) {
    logger.warn("[api-proxy] Upstream request failed before receiving an HTTP response.", {
      reason: "upstream_fetch_failed",
      method: request.method,
      error_name: error instanceof Error ? error.name : "UnknownError",
    });
    return Response.json(
      { error: { code: "API_PROXY_UNAVAILABLE", message: "API temporarily unavailable." } },
      { status: 502 },
    );
  }
}
