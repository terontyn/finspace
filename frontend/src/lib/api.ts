import type { ApiErrorResponse, ReadinessResponse, SystemStatus } from "@/types/system";
import { buildApiUrl, publicApiUrl } from "./api-url";

export { publicApiUrl } from "./api-url";

export interface StatusResult {
  status: SystemStatus;
  error: string | null;
}

function dependencyStatus(
  value: "ok" | "unavailable" | undefined,
): "ok" | "unavailable" {
  return value === "ok" ? "ok" : "unavailable";
}

export async function fetchSystemStatus(signal?: AbortSignal): Promise<StatusResult> {
  try {
    const response = await fetch(buildApiUrl(publicApiUrl, "/api/v1/health/ready"), {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal,
    });
    const payload = (await response.json()) as ReadinessResponse | ApiErrorResponse;

    if (response.ok && "status" in payload && payload.status === "ready") {
      return {
        status: { backend: "ok", database: "ok", redis: "ok" },
        error: null,
      };
    }

    const apiError = "error" in payload ? payload.error : undefined;
    const checks = apiError?.details?.checks;
    return {
      status: {
        backend: "ok",
        database: dependencyStatus(checks?.database),
        redis: dependencyStatus(checks?.redis),
      },
      error: apiError?.message ?? `Backend returned HTTP ${response.status}`,
    };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    return {
      status: { backend: "unavailable", database: "unavailable", redis: "unavailable" },
      error: "Не удалось подключиться к backend. Проверьте Docker Compose и URL API.",
    };
  }
}
