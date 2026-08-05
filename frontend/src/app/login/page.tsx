"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";

import { useAuth } from "@/components/auth-provider";
import { ApiClientError, apiClient } from "@/lib/api-client";

export default function LoginPage() {
  const auth = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [developmentUserId, setDevelopmentUserId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!auth.loading && auth.session) window.location.replace("/");
  }, [auth.loading, auth.session]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.login(email, password);
      window.location.replace("/");
    } catch (requestError) {
      setError(
        requestError instanceof ApiClientError
          ? requestError.message
          : "Неверный email или пароль. Попробуйте ещё раз.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function bootstrap() {
    setBusy(true);
    setError(null);
    try {
      const result = await apiClient.bootstrap();
      setDevelopmentUserId(result.user_id);
      setEmail("developer@finspace.local");
    } catch (requestError) {
      setError(
        requestError instanceof ApiClientError
          ? requestError.message
          : "Не удалось создать тестового пользователя.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function setDevPassword() {
    if (!developmentUserId) return;
    setBusy(true);
    setError(null);
    try {
      await auth.setDevelopmentPassword(developmentUserId, password);
      window.location.replace("/");
    } catch (requestError) {
      setError(
        requestError instanceof ApiClientError ? requestError.message : "Не удалось задать пароль.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card">
        {/* Логотип */}
        <div className="brand-logo">Ф</div>

        <span className="kicker">Личный финансовый трекер</span>
        <h1>Добро пожаловать</h1>
        <p>Войдите, чтобы продолжить управлять своими финансами.</p>

        {error ? (
          <div className="notice notice--error" role="alert">
            {error}
          </div>
        ) : null}

        <form className="auth-form" onSubmit={(event) => void submit(event)}>
          <label>
            Email
            <input
              type="email"
              autoComplete="email"
              placeholder="you@example.com"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </label>
          <label>
            Пароль
            <input
              type="password"
              minLength={10}
              maxLength={128}
              autoComplete="current-password"
              placeholder="Минимум 10 символов"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          <button type="submit" disabled={busy} style={{ marginTop: 4 }}>
            {busy ? "Проверяем…" : "Войти в аккаунт →"}
          </button>
        </form>

        {developmentUserId ? (
          <button
            className="secondary-button auth-wide-button"
            type="button"
            disabled={busy || password.length < 10}
            onClick={() => void setDevPassword()}
          >
            Задать этот пароль dev-пользователю
          </button>
        ) : process.env.NODE_ENV === "development" ? (
          <button
            className="text-button auth-wide-button"
            type="button"
            disabled={busy}
            onClick={() => void bootstrap()}
          >
            Создать dev-пользователя
          </button>
        ) : null}

        <small>
          Нет аккаунта?{" "}
          <Link href="/register">Создать пространство</Link>
        </small>
      </section>
    </main>
  );
}
