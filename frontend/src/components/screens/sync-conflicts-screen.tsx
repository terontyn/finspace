"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiClient } from "@/lib/api-client";
import {
  conflictEntityLabel,
  conflictFieldDiffs,
  conflictResolutionMessage,
  conflictValue,
  parseMergedPayload,
} from "@/lib/google-sync";
import type { ConflictPage, SyncConflict } from "@/types/google";

interface Props {
  onError: (error: unknown) => void;
}

type ConflictFilter = "open" | "resolved" | "all";

export function SyncConflictsScreen({ onError }: Props) {
  const [items, setItems] = useState<SyncConflict[]>([]);
  const [selected, setSelected] = useState<SyncConflict | null>(null);
  const [mergedPayload, setMergedPayload] = useState("{}");
  const [filter, setFilter] = useState<ConflictFilter>("open");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (nextFilter: ConflictFilter) => {
    setLoading(true);
    try {
      const status = nextFilter === "all" ? "" : `&status=${nextFilter}`;
      const page = await apiClient.get<ConflictPage>(
        `/api/v1/google-sheets/conflicts?limit=100&offset=0${status}`,
      );
      setItems(page.items);
      setSelected((current) => page.items.find((item) => item.id === current?.id) ?? null);
    } catch (error) {
      onError(error);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load(filter));
  }, [filter, load]);

  const diffs = useMemo(
    () => selected ? conflictFieldDiffs(selected) : [],
    [selected],
  );

  function selectConflict(item: SyncConflict): void {
    setSelected(item);
    const suggested = item.sheet_payload.changed_fields ?? item.database_payload;
    setMergedPayload(JSON.stringify(suggested, null, 2));
  }

  async function resolve(
    resolution: "keep_database" | "keep_sheet" | "manual_merge",
  ): Promise<void> {
    if (!selected || busy) return;
    let body: { resolution: string; merged_payload?: Record<string, unknown> } = { resolution };
    if (resolution === "manual_merge") {
      try {
        body = { resolution, merged_payload: parseMergedPayload(mergedPayload) };
      } catch (error) {
        onError(error);
        return;
      }
    }
    if (!window.confirm(conflictResolutionMessage(resolution))) return;
    setBusy(true);
    try {
      await apiClient.post(`/api/v1/google-sheets/conflicts/${selected.id}/resolve`, body);
      setSelected(null);
      await load(filter);
    } catch (error) {
      onError(error);
      await load(filter);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Google Sheets → проверка изменений</span>
          <h1>Конфликты синхронизации</h1>
          <p>Finspace не применяет устаревшие или противоречивые данные автоматически.</p>
        </div>
      </header>

      <div className="notice">
        Решение выполняется одной backend-командой: сущность, audit, outbox и статус конфликта не расходятся.
      </div>

      <div className="conflict-toolbar">
        <label>
          Состояние
          <select value={filter} onChange={(event) => {
            const value = event.target.value as ConflictFilter;
            setFilter(value);
            setSelected(null);
          }}>
            <option value="open">Требуют решения</option>
            <option value="resolved">Разрешённые</option>
            <option value="all">Все</option>
          </select>
        </label>
        <button className="secondary-button" disabled={loading || busy} onClick={() => void load(filter)} type="button">Обновить</button>
      </div>

      <div className="two-column conflict-layout">
        <article className="panel">
          <div className="panel-heading"><h2>{filter === "open" ? "Требуют решения" : "История"}</h2><span className="count-badge">{items.length}</span></div>
          {loading ? <div className="empty-state">Загружаем конфликты…</div> : null}
          {!loading && !items.length ? <div className="empty-state"><strong>{filter === "open" ? "Всё синхронизировано" : "Конфликтов нет"}</strong><span>{filter === "open" ? "Открытых расхождений не найдено." : "Для выбранного состояния записей нет."}</span></div> : null}
          <div className="card-list">
            {items.map((item) => (
              <button className={selected?.id === item.id ? "conflict-card conflict-card--active" : "conflict-card"} key={item.id} onClick={() => selectConflict(item)} type="button">
                <span className="conflict-card-heading"><strong>{conflictEntityLabel(item.entity_type)}</strong><span className={`status-chip status-chip--${item.status}`}>{item.status === "open" ? "Нужно решение" : "Разрешён"}</span></span>
                <span>{item.conflicting_fields.join(", ") || "Изменённые поля"}</span>
                <small>Google Sheets · {new Date(item.created_at).toLocaleString("ru-RU")}</small>
              </button>
            ))}
          </div>
        </article>

        <article className="panel diff-panel">
          {selected ? <>
            <div className="panel-heading"><div><span className="kicker">Сравнение</span><h2>{conflictEntityLabel(selected.entity_type)}</h2></div><span className={`status-chip status-chip--${selected.status}`}>{selected.status === "open" ? "Открыт" : "Разрешён"}</span></div>
            <div className="version-pair"><span>Finspace v{selected.database_version}</span><span>Google Sheets v{selected.sheet_version ?? "—"}</span></div>
            <div className="conflict-field-table" role="table" aria-label="Различия конфликта">
              <div className="conflict-field-row conflict-field-row--header" role="row"><span>Поле</span><span>Finspace</span><span>Google Sheets</span></div>
              {diffs.map((diff) => <div className="conflict-field-row" key={diff.field} role="row"><strong>{diff.label}</strong><span>{conflictValue(diff.database)}</span><span>{conflictValue(diff.external)}</span></div>)}
            </div>
            {!diffs.length ? <div className="empty-state">Backend не указал отдельные конфликтующие поля.</div> : null}

            {selected.status === "open" ? <>
              <div className="workflow-actions conflict-actions">
                <button disabled={busy} onClick={() => void resolve("keep_database")} type="button">Оставить Finspace</button>
                <button className="secondary-button" disabled={busy} onClick={() => void resolve("keep_sheet")} type="button">Принять Google</button>
              </div>
              <details className="manual-resolution">
                <summary>Ручное объединение</summary>
                <label className="field"><span>JSON только с изменяемыми полями</span><textarea rows={8} value={mergedPayload} onChange={(event) => setMergedPayload(event.target.value)} /></label>
                <button className="secondary-button" disabled={busy} onClick={() => void resolve("manual_merge")} type="button">Применить объединение</button>
              </details>
            </> : <p>Решение: {selected.resolution}; {selected.resolved_at ? new Date(selected.resolved_at).toLocaleString("ru-RU") : "—"}</p>}

            <details className="conflict-diagnostics">
              <summary>Технические snapshots</summary>
              <div className="diff-grid"><div><h3>Finspace</h3><pre>{JSON.stringify(selected.database_payload, null, 2)}</pre></div><div><h3>Google Sheets</h3><pre>{JSON.stringify(selected.sheet_payload, null, 2)}</pre></div></div>
            </details>
          </> : <div className="empty-state"><strong>Выберите конфликт</strong><span>Здесь появится сравнение полей и безопасные варианты решения.</span></div>}
        </article>
      </div>
    </section>
  );
}
