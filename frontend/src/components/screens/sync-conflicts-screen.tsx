"use client";

import { useCallback, useEffect, useState } from "react";

import { apiClient } from "@/lib/api-client";
import { conflictResolutionMessage, parseMergedPayload } from "@/lib/google-sync";
import type { ConflictPage, SyncConflict } from "@/types/google";

interface Props { onError: (error: unknown) => void }

export function SyncConflictsScreen({ onError }: Props) {
  const [items, setItems] = useState<SyncConflict[]>([]);
  const [selected, setSelected] = useState<SyncConflict | null>(null);
  const [mergedPayload, setMergedPayload] = useState("{}");

  const load = useCallback(async () => {
    try {
      const page = await apiClient.get<ConflictPage>("/api/v1/google-sheets/conflicts?limit=100&offset=0");
      setItems(page.items);
    } catch (error) { onError(error); }
  }, [onError]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  function selectConflict(item: SyncConflict): void {
    setSelected(item);
    const suggested = item.sheet_payload.changed_fields ?? item.database_payload;
    setMergedPayload(JSON.stringify(suggested, null, 2));
  }

  async function resolve(
    resolution: "keep_database" | "keep_sheet" | "manual_merge",
  ): Promise<void> {
    if (!selected) return;
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
    try {
      await apiClient.post(`/api/v1/google-sheets/conflicts/${selected.id}/resolve`, body);
      setSelected(null);
      await load();
    } catch (error) { onError(error); }
  }

  return (
    <section>
      <header className="screen-header"><div><span className="kicker">Google Sheets</span><h1>Конфликты</h1><p>Устаревшие строки не перезаписывают PostgreSQL автоматически.</p></div></header>
      <div className="two-column conflict-layout">
        <article className="panel">
          <div className="panel-heading"><h2>Очередь конфликтов</h2><span className="count-badge">{items.length}</span></div>
          <div className="card-list">
            {items.length ? items.map((item) => (
              <button className={selected?.id === item.id ? "conflict-card conflict-card--active" : "conflict-card"} key={item.id} onClick={() => selectConflict(item)} type="button">
                <strong>{item.entity_type}</strong><span>{item.entity_id}</span><small>{item.conflicting_fields.join(", ")} · {item.status}</small>
              </button>
            )) : <div className="empty-state">Открытых конфликтов нет.</div>}
          </div>
        </article>
        <article className="panel diff-panel">
          {selected ? <>
            <div className="panel-heading"><div><span className="kicker">Diff</span><h2>{selected.entity_type}</h2></div></div>
            <div className="version-pair"><span>PostgreSQL v{selected.database_version}</span><span>Sheets v{selected.sheet_version ?? "—"}</span></div>
            <p>Создан: {new Date(selected.created_at).toLocaleString("ru-RU")}</p>
            <div className="diff-grid"><div><h3>PostgreSQL</h3><pre>{JSON.stringify(selected.database_payload, null, 2)}</pre></div><div><h3>Google Sheets</h3><pre>{JSON.stringify(selected.sheet_payload, null, 2)}</pre></div></div>
            {selected.status === "open" ? <>
              <label className="field"><span>Ручное объединение (JSON изменяемых полей)</span><textarea rows={8} value={mergedPayload} onChange={(event) => setMergedPayload(event.target.value)} /></label>
              <div className="workflow-actions"><button onClick={() => void resolve("keep_database")} type="button">Оставить PostgreSQL</button><button className="secondary-button" onClick={() => void resolve("keep_sheet")} type="button">Оставить Google Sheets</button><button className="secondary-button" onClick={() => void resolve("manual_merge")} type="button">Объединить вручную</button></div>
            </> : <p>Решение: {selected.resolution}; {selected.resolved_at ? new Date(selected.resolved_at).toLocaleString("ru-RU") : "—"}</p>}
          </> : <div className="empty-state">Выберите конфликт, чтобы увидеть diff.</div>}
        </article>
      </div>
    </section>
  );
}
