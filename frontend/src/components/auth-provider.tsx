"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import {
  ApiClientError,
  apiClient,
  type AuthMeResponse,
  type AuthSession,
  type WorkspaceRole,
} from "@/lib/api-client";
import { restoreAuthState } from "@/lib/auth-session";

interface AuthContextValue {
  loading: boolean;
  role: WorkspaceRole | null;
  roleLoading: boolean;
  session: AuthSession | null;
  login: (email: string, password: string) => Promise<void>;
  register: (data: Record<string, string>) => Promise<void>;
  setDevelopmentPassword: (userId: string, password: string) => Promise<void>;
  logout: (all?: boolean) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [role, setRole] = useState<WorkspaceRole | null>(null);
  const [roleLoading, setRoleLoading] = useState(false);
  const [session, setSession] = useState<AuthSession | null>(null);

  useEffect(() => {
    let mounted = true;
    const redirectToLogin = () => {
      if (
        !window.location.pathname.startsWith("/login") &&
        !window.location.pathname.startsWith("/register")
      ) {
        window.location.replace("/login");
      }
    };

    apiClient.setSessionExpiredHandler(() => {
      if (!mounted) return;
      setSession(null);
      redirectToLogin();
    });
    void restoreAuthState({
      restoreSession: () => apiClient.restoreSession(),
      isMounted: () => mounted,
      setSession,
      setLoading,
      redirectToLogin,
    });
    return () => {
      mounted = false;
      apiClient.setSessionExpiredHandler(null);
    };
  }, []);

  useEffect(() => {
    let mounted = true;
    if (!session) {
      queueMicrotask(() => {
        if (!mounted) return;
        setRole(null);
        setRoleLoading(false);
      });
      return () => { mounted = false; };
    }

    queueMicrotask(() => {
      if (!mounted) return;
      setRole(null);
      setRoleLoading(true);
    });
    void apiClient.get<AuthMeResponse>("/api/v1/auth/me").then((response) => {
      if (mounted) setRole(response.role);
    }).catch((error: unknown) => {
      if (!mounted) return;
      setRole(null);
      console.warn("[auth] Не удалось получить роль в рабочем пространстве.", {
        kind: error instanceof ApiClientError ? "api_error" : "unexpected_error",
        code: error instanceof ApiClientError ? error.code : undefined,
        status: error instanceof ApiClientError ? error.status : undefined,
        requestId: error instanceof ApiClientError ? error.requestId : undefined,
      });
    }).finally(() => {
      if (mounted) setRoleLoading(false);
    });

    return () => { mounted = false; };
  }, [session]);

  const value = useMemo<AuthContextValue>(
    () => ({
      loading,
      role,
      roleLoading,
      session,
      login: async (email, password) => setSession(await apiClient.login(email, password)),
      register: async (data) => setSession(await apiClient.register(data)),
      setDevelopmentPassword: async (userId, password) =>
        setSession(await apiClient.setDevelopmentPassword(userId, password)),
      logout: async (all = false) => {
        await apiClient.logout(all);
        setSession(null);
      },
    }),
    [loading, role, roleLoading, session],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
