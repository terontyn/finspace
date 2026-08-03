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
      setError(requestError instanceof ApiClientError ? requestError.message : "Не удалось войти.");
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
          : "Development bootstrap недоступен.",
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
        <div className="brand-symbol">Ф</div>
        <span className="kicker">Локальная аутентификация</span>
        <h1>С возвращением</h1>
        <p>Access token хранится только в памяти браузера, а сессия безопасно обновляется cookie.</p>
        {error ? <div className="notice notice--error">{error}</div> : null}
        <form className="auth-form" onSubmit={(event) => void submit(event)}>
          <label>
            Email
            <input type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
          </label>
          <label>
            Пароль
            <input type="password" minLength={10} maxLength={128} autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
          </label>
          <button type="submit" disabled={busy}>{busy ? "Проверяем…" : "Войти"}</button>
        </form>
        {developmentUserId ? (
          <button className="secondary-button auth-wide-button" type="button" disabled={busy || password.length < 10} onClick={() => void setDevPassword()}>
            Задать этот пароль dev-пользователю
          </button>
        ) : process.env.NODE_ENV === "development" ? (
          <button className="text-button auth-wide-button" type="button" disabled={busy} onClick={() => void bootstrap()}>
            Подготовить development-пользователя
          </button>
        ) : null}
        <small>Нет пространства? <Link href="/register">Создать аккаунт</Link></small>
      </section>
    </main>
  );
}
