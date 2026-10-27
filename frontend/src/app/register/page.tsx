"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";

import { useAuth } from "@/components/auth-provider";
import { ApiClientError } from "@/lib/api-client";

export default function RegisterPage() {
  const auth = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    email: "",
    display_name: "",
    password: "",
    workspace_name: "Личные финансы",
    base_currency: "RUB",
    timezone: "Europe/Amsterdam",
  });

  useEffect(() => {
    if (!auth.loading && auth.session) window.location.replace("/");
  }, [auth.loading, auth.session]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.register(form);
      window.location.replace("/");
    } catch (requestError) {
      setError(
        requestError instanceof ApiClientError ? requestError.message : "Не удалось зарегистрироваться.",
      );
    } finally {
      setBusy(false);
    }
  }

  function field(name: keyof typeof form, value: string) {
    setForm((current) => ({ ...current, [name]: value }));
  }

  return (
    <main className="auth-shell">
      <section className="auth-card auth-card--wide">
        <div className="brand-symbol">Ф</div>
        <span className="kicker">Новое пространство</span>
        <h1>Начать с чистого листа</h1>
        {error ? <div className="notice notice--error">{error}</div> : null}
        <form className="auth-form auth-form--grid" onSubmit={(event) => void submit(event)}>
          <label>Имя<input value={form.display_name} onChange={(event) => field("display_name", event.target.value)} required /></label>
          <label>Email<input type="email" autoComplete="email" value={form.email} onChange={(event) => field("email", event.target.value)} required /></label>
          <label className="auth-span-two">Пароль<input type="password" minLength={10} maxLength={128} autoComplete="new-password" value={form.password} onChange={(event) => field("password", event.target.value)} required /></label>
          <label>Название пространства<input value={form.workspace_name} onChange={(event) => field("workspace_name", event.target.value)} required /></label>
          <label>Валюта<select value={form.base_currency} onChange={(event) => field("base_currency", event.target.value)}><option>RUB</option><option>EUR</option><option>USD</option></select></label>
          <label className="auth-span-two">Часовой пояс<input value={form.timezone} onChange={(event) => field("timezone", event.target.value)} required /></label>
          <button className="auth-span-two" type="submit" disabled={busy}>{busy ? "Создаём…" : "Создать пространство"}</button>
        </form>
        <small>Уже зарегистрированы? <Link href="/login">Войти</Link></small>
      </section>
    </main>
  );
}
