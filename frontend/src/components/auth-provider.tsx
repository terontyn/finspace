"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { apiClient, type AuthSession } from "@/lib/api-client";

interface AuthContextValue {
  loading: boolean;
  session: AuthSession | null;
  login: (email: string, password: string) => Promise<void>;
  register: (data: Record<string, string>) => Promise<void>;
  setDevelopmentPassword: (userId: string, password: string) => Promise<void>;
  logout: (all?: boolean) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [session, setSession] = useState<AuthSession | null>(null);

  useEffect(() => {
    apiClient.setSessionExpiredHandler(() => {
      setSession(null);
      if (!window.location.pathname.startsWith("/login")) window.location.assign("/login");
    });
    void apiClient.restoreSession().then((restored) => {
      setSession(restored);
      setLoading(false);
    });
    return () => apiClient.setSessionExpiredHandler(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      loading,
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
    [loading, session],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
