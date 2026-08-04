import { ApiClientError, type AuthSession } from "./api-client.ts";

interface AuthRestoreLogger {
  warn(message: string, context: Record<string, unknown>): void;
}

interface RestoreAuthStateOptions {
  restoreSession: () => Promise<AuthSession | null>;
  isMounted: () => boolean;
  setSession: (session: AuthSession | null) => void;
  setLoading: (loading: boolean) => void;
  redirectToLogin: () => void;
  logger?: AuthRestoreLogger;
}

function safeErrorContext(error: unknown): Record<string, unknown> {
  if (error instanceof ApiClientError) {
    return {
      kind: "api_error",
      code: error.code,
      status: error.status,
      requestId: error.requestId,
    };
  }
  if (error instanceof Error && error.name === "AbortError") {
    return { kind: "timeout_or_abort" };
  }
  return { kind: "unexpected_error" };
}

export async function restoreAuthState({
  restoreSession,
  isMounted,
  setSession,
  setLoading,
  redirectToLogin,
  logger = console,
}: RestoreAuthStateOptions): Promise<void> {
  try {
    const restored = await restoreSession();
    if (!isMounted()) return;
    setSession(restored);
    if (restored === null) redirectToLogin();
  } catch (error) {
    if (!isMounted()) return;
    logger.warn("[auth] Не удалось восстановить авторизационную сессию.", safeErrorContext(error));
    setSession(null);
    redirectToLogin();
  } finally {
    if (isMounted()) setLoading(false);
  }
}
