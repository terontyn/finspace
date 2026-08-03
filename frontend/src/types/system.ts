export type CheckStatus = "checking" | "ok" | "unavailable";

export interface ReadinessResponse {
  status: "ready";
  checks: {
    database: "ok";
    redis: "ok";
  };
}

export interface ApiErrorResponse {
  error?: {
    code?: string;
    message?: string;
    request_id?: string;
    details?: {
      checks?: {
        database?: "ok" | "unavailable";
        redis?: "ok" | "unavailable";
      };
    };
  };
}

export interface SystemStatus {
  backend: CheckStatus;
  database: CheckStatus;
  redis: CheckStatus;
}
