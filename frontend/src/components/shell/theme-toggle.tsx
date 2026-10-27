"use client";

import { useEffect, useState } from "react";

type Theme = "dark" | "light";

export function resolveStoredTheme(value: string | null): Theme {
  return value === "light" ? "light" : "dark";
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    queueMicrotask(() => {
      const initial = resolveStoredTheme(window.localStorage.getItem("finspace-theme"));
      setTheme(initial);
      applyTheme(initial);
    });
  }, []);

  function toggle() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    window.localStorage.setItem("finspace-theme", next);
    applyTheme(next);
  }

  return (
    <button className="shell-icon-button" onClick={toggle} type="button" aria-label={theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему"}>
      {theme === "dark" ? (
        <svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
      ) : (
        <svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18"><path d="M20.5 15.7A9 9 0 0 1 8.3 3.5a9 9 0 1 0 12.2 12.2Z"/></svg>
      )}
    </button>
  );
}
