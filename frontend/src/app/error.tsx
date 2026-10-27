"use client";

import { useEffect } from "react";

export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => { console.error("[ui] Route rendering failed", { name: error.name, digest: error.digest ?? null }); }, [error]);
  return <main className="loading-page"><section className="route-error" role="alert"><span className="kicker">Ошибка интерфейса</span><h1>Раздел не удалось открыть</h1><p>Данные и сессия не изменены. Повторите загрузку раздела.</p><button className="primary-button" onClick={reset} type="button">Повторить</button></section></main>;
}
