export const publicApiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  locale: string;
  timezone: string;
  is_active: boolean;
  version: number;
}

export interface AuthWorkspace {
  id: string;
  name: string;
  base_currency: string;
  timezone: string;
  owner_user_id: string;
  version: number;
}

export interface AuthSession {
  accessToken: string;
  expiresIn: number;
  user: AuthUser;
  workspace: AuthWorkspace;
}

interface AuthResponse {
  access_token: string;
  expires_in: number;
  user: AuthUser;
  workspace: AuthWorkspace;
}

interface ApiErrorPayload {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
    request_id?: string;
  };
}

export class ApiClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId?: string;
  readonly details?: unknown;

  constructor(
    message: string,
    code: string,
    status: number,
    requestId?: string,
    details?: unknown,
  ) {
    super(message);
    this.name = "ApiClientError";
    this.code = code;
    this.status = status;
    this.requestId = requestId;
    this.details = details;
  }
}

export class ApiClient {
  private readonly fetcher: typeof fetch;
  private session: AuthSession | null = null;
  private refreshPromise: Promise<boolean> | null = null;
  private sessionExpiredHandler: (() => void) | null = null;

  constructor(fetcher: typeof fetch = fetch) {
    this.fetcher = fetcher;
  }

  getSession(): AuthSession | null {
    return this.session;
  }

  setSessionExpiredHandler(handler: (() => void) | null): void {
    this.sessionExpiredHandler = handler;
  }

  private acceptAuth(response: AuthResponse): AuthSession {
    this.session = {
      accessToken: response.access_token,
      expiresIn: response.expires_in,
      user: response.user,
      workspace: response.workspace,
    };
    return this.session;
  }

  clearSession(): void {
    this.session = null;
  }

  private async parseResponse<T>(response: Response): Promise<T> {
    const payload = (await response.json().catch(() => ({}))) as T & ApiErrorPayload;
    if (!response.ok) {
      throw new ApiClientError(
        payload.error?.message ?? `API вернул HTTP ${response.status}`,
        payload.error?.code ?? "API_ERROR",
        response.status,
        payload.error?.request_id ?? response.headers.get("X-Request-ID") ?? undefined,
        payload.error?.details,
      );
    }
    return payload;
  }

  private async send<T>(path: string, init: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await this.fetcher(`${publicApiUrl}${path}`, {
        ...init,
        cache: "no-store",
        credentials: "include",
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiClientError(
        "Backend недоступен. Проверьте Docker Compose и адрес API.",
        "API_UNAVAILABLE",
        0,
      );
    }
    return this.parseResponse<T>(response);
  }

  private async refreshAccess(notifyOnFailure: boolean): Promise<boolean> {
    if (this.refreshPromise) return this.refreshPromise;
    this.refreshPromise = (async () => {
      try {
        const response = await this.send<AuthResponse>("/api/v1/auth/refresh", {
          method: "POST",
          headers: { Accept: "application/json" },
        });
        this.acceptAuth(response);
        return true;
      } catch {
        this.clearSession();
        if (notifyOnFailure) this.sessionExpiredHandler?.();
        return false;
      } finally {
        this.refreshPromise = null;
      }
    })();
    return this.refreshPromise;
  }

  async restoreSession(): Promise<AuthSession | null> {
    await this.refreshAccess(false);
    return this.session;
  }

  async request<T>(path: string, init: RequestInit = {}, allowRefresh = true): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
    if (init.body && !isFormData) headers.set("Content-Type", "application/json");
    if (this.session) {
      headers.set("Authorization", `Bearer ${this.session.accessToken}`);
      headers.set("X-Workspace-ID", this.session.workspace.id);
    }

    try {
      return await this.send<T>(path, { ...init, headers });
    } catch (error) {
      if (
        error instanceof ApiClientError &&
        error.status === 401 &&
        allowRefresh &&
        !path.startsWith("/api/v1/auth/")
      ) {
        if (await this.refreshAccess(true)) return this.request<T>(path, init, false);
      }
      throw error;
    }
  }

  async login(email: string, password: string): Promise<AuthSession> {
    const response = await this.request<AuthResponse>(
      "/api/v1/auth/login",
      { method: "POST", body: JSON.stringify({ email, password }) },
      false,
    );
    return this.acceptAuth(response);
  }

  async register(data: Record<string, string>): Promise<AuthSession> {
    const response = await this.request<AuthResponse>(
      "/api/v1/auth/register",
      { method: "POST", body: JSON.stringify(data) },
      false,
    );
    return this.acceptAuth(response);
  }

  async setDevelopmentPassword(userId: string, password: string): Promise<AuthSession> {
    const response = await this.request<AuthResponse>(
      "/api/v1/auth/set-development-password",
      { method: "POST", body: JSON.stringify({ user_id: userId, password }) },
      false,
    );
    return this.acceptAuth(response);
  }

  bootstrap(): Promise<{ user_id: string; workspace_id: string; created: boolean }> {
    return this.request("/api/v1/dev/bootstrap", { method: "POST" }, false);
  }

  async logout(all = false): Promise<void> {
    try {
      await this.request(
        all ? "/api/v1/auth/logout-all" : "/api/v1/auth/logout",
        { method: "POST" },
        false,
      );
    } finally {
      this.clearSession();
    }
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>(path);
  }

  post<T>(path: string, body?: unknown, headers?: HeadersInit): Promise<T> {
    return this.request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
      headers,
    });
  }

  put<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(path, { method: "PUT", body: JSON.stringify(body) });
  }

  patch<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
  }

  delete<T>(path: string): Promise<T> {
    return this.request<T>(path, { method: "DELETE" });
  }

  upload<T>(path: string, form: FormData): Promise<T> {
    return this.request<T>(path, { method: "POST", body: form });
  }
}

export const apiClient = new ApiClient();
