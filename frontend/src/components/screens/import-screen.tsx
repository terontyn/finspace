"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiClient } from "@/lib/api-client";
import type { ImportBatch, ImportRow, Paged } from "@/types/finance";

interface ImportScreenProps {
  onError: (error: unknown) => void;
}

const mappingTargets = [
  ["date", "Дата"],
  ["time", "Время"],
  ["transaction_type", "Тип операции"],
  ["amount", "Сумма"],
  ["income_amount", "Сумма дохода"],
  ["expense_amount", "Сумма расхода"],
  ["currency", "Валюта"],
  ["account", "Счёт"],
  ["target_account", "Целевой счёт"],
  ["category", "Категория"],
  ["counterparty", "Контрагент"],
  ["description", "Описание"],
  ["comment", "Комментарий"],
  ["status", "Статус"],
  ["external_id", "Внешний ID"],
] as const;

function number(summary: Record<string, unknown> | null, key: string): number {
  const value = summary?.[key];
  return typeof value === "number" ? value : 0;
}

function strings(summary: Record<string, unknown> | null, key: string): string[] {
  const value = summary?.[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function formatSize(bytes: number): string {
  return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} КБ` : `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

export function ImportScreen({ onError }: ImportScreenProps) {
  const [batches, setBatches] = useState<ImportBatch[]>([]);
  const [active, setActive] = useState<ImportBatch | null>(null);
  const [rows, setRows] = useState<ImportRow[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [forceDuplicate, setForceDuplicate] = useState(false);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [locale, setLocale] = useState("ru-RU");
  const [rowFilter, setRowFilter] = useState("all");
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  const loadBatches = useCallback(async () => {
    const result = await apiClient.get<Paged<ImportBatch>>("/api/v1/imports?limit=50");
    setBatches(result.items);
  }, []);

  const loadRows = useCallback(async (batchId: string, filter = rowFilter) => {
    const query = filter === "all" ? "" : filter === "errors" ? "?has_errors=true" : filter === "duplicate" ? "?duplicate=true" : `?status=${filter}`;
    const result = await apiClient.get<Paged<ImportRow>>(`/api/v1/imports/${batchId}/rows${query}`);
    setRows(result.items);
  }, [rowFilter]);

  useEffect(() => {
    queueMicrotask(() => void loadBatches().catch(onError));
  }, [loadBatches, onError]);

  const columns = useMemo(() => strings(active?.summary ?? null, "source_columns"), [active]);

  function selectBatch(batch: ImportBatch) {
    setActive(batch);
    setMapping(batch.mapping?.fields ?? {});
    setLocale(batch.mapping?.locale ?? "ru-RU");
    setConfirmed(false);
    void loadRows(batch.id).catch(onError);
  }

  async function run(action: () => Promise<ImportBatch>) {
    setBusy(true);
    try {
      const result = await action();
      setActive(result);
      await Promise.all([loadBatches(), loadRows(result.id)]);
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }

  async function upload() {
    if (!file) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.set("file", file);
      form.set("force_duplicate", String(forceDuplicate));
      const batch = await apiClient.upload<ImportBatch>("/api/v1/imports", form);
      setFile(null);
      selectBatch(batch);
      await loadBatches();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    if (!active || !confirmed) return;
    const idempotencyKey = `import-${active.id}`;
    await run(async () => {
      const result = await apiClient.post<{ batch: ImportBatch; affected_transactions: number }>(
        `/api/v1/imports/${active.id}/commit`,
        { confirm: true },
        { "X-Idempotency-Key": idempotencyKey },
      );
      return result.batch;
    });
  }

  async function importDuplicateAsNew(row: ImportRow) {
    if (!active) return;
    setBusy(true);
    try {
      await apiClient.patch(`/api/v1/imports/${active.id}/rows/${row.id}`, {
        import_as_new: true,
      });
      const refreshed = await apiClient.get<ImportBatch>(`/api/v1/imports/${active.id}`);
      setActive(refreshed);
      await loadRows(active.id, rowFilter);
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }

  const excluded = active
    ? number(active.summary, "invalid") + number(active.summary, "duplicate") + number(active.summary, "skipped")
    : 0;

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Безопасный staging</span>
          <h1>Импорт</h1>
          <p>CSV и XLSX проходят сопоставление и проверку до появления финансовых операций.</p>
        </div>
      </header>

      <div className="panel import-upload">
        <div>
          <strong>1. Загрузка файла</strong>
          <p>Файл получает случайное внутреннее имя. На этом шаге в transactions ничего не записывается.</p>
        </div>
        <input type="file" accept=".csv,.xlsx" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        {file ? <span className="file-summary">{file.name} · {formatSize(file.size)}</span> : null}
        <label className="toggle"><input type="checkbox" checked={forceDuplicate} onChange={(event) => setForceDuplicate(event.target.checked)} />Осознанно создать новый batch для уже загруженного файла</label>
        <button type="button" disabled={!file || busy} onClick={() => void upload()}>Загрузить в staging</button>
      </div>

      {active ? (
        <div className="import-workflow">
          <section className="panel">
            <div className="panel-heading">
              <div><span className="kicker">Batch {active.id.slice(0, 8)}</span><h2>{active.filename}</h2><p>{formatSize(active.file_size)} · {active.status}</p></div>
              <span className={`status-chip status-chip--${active.status}`}>{active.status}</span>
            </div>
            <h3>2. Сопоставление</h3>
            <p className="section-copy">Обязательны дата, счёт и сумма — единая либо отдельные колонки дохода/расхода.</p>
            <div className="mapping-grid">
              {mappingTargets.map(([target, label]) => (
                <label key={target}>{label}<select value={mapping[target] ?? ""} onChange={(event) => setMapping((current) => ({ ...current, [target]: event.target.value }))}><option value="">Не импортировать</option>{columns.map((column) => <option key={column} value={column}>{column}</option>)}</select></label>
              ))}
              <label>Локаль<select value={locale} onChange={(event) => setLocale(event.target.value)}><option value="ru-RU">ru-RU — день.месяц</option><option value="en-US">en-US — month/day</option></select></label>
            </div>
            <div className="workflow-actions">
              <button className="secondary-button" type="button" disabled={busy} onClick={() => void run(() => apiClient.put(`/api/v1/imports/${active.id}/mapping`, { mapping: Object.fromEntries(Object.entries(mapping).filter(([, value]) => value)), locale }))}>Сохранить mapping</button>
              <button type="button" disabled={busy || active.status === "mapping_required"} onClick={() => void run(() => apiClient.post(`/api/v1/imports/${active.id}/validate`))}>3. Проверить строки</button>
            </div>
          </section>

          <section className="panel">
            <div className="panel-heading"><div><span className="kicker">Preview</span><h2>Результат проверки</h2></div></div>
            <div className="import-metrics">
              <div><span>Valid</span><strong>{number(active.summary, "valid")}</strong></div>
              <div><span>Invalid</span><strong>{number(active.summary, "invalid")}</strong></div>
              <div><span>Duplicate</span><strong>{number(active.summary, "duplicate")}</strong></div>
              <div><span>Skipped</span><strong>{number(active.summary, "skipped")}</strong></div>
            </div>
            <div className="filters import-filters">
              <select value={rowFilter} onChange={(event) => { const value = event.target.value; setRowFilter(value); if (active) void loadRows(active.id, value).catch(onError); }}><option value="all">Все строки</option><option value="valid">Valid</option><option value="errors">С ошибками</option><option value="duplicate">Дубликаты</option><option value="skipped">Skipped</option></select>
            </div>
            <div className="table-scroll"><table><thead><tr><th>Строка</th><th>Статус</th><th>Исходные данные</th><th>Ошибки</th><th>Решение</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td>{row.source_sheet ? `${row.source_sheet}:` : ""}{row.source_row_number}</td><td><span className={`status-chip status-chip--${row.status}`}>{row.status}</span></td><td>{Object.entries(row.raw_data).map(([key, value]) => `${key}: ${String(value ?? "")}`).join(" · ")}</td><td>{row.validation_errors?.map((item) => item.message ?? item.code).join("; ") ?? "—"}</td><td>{row.status === "duplicate" ? <button className="text-button" type="button" disabled={busy} onClick={() => void importDuplicateAsNew(row)}>Это новая операция</button> : "—"}</td></tr>)}</tbody></table></div>
          </section>

          <section className="panel confirmation-panel">
            <div><span className="kicker">4. Подтверждение</span><h2>Будет создано {number(active.summary, "valid")} операций</h2><p>Счета: {strings(active.summary, "accounts").join(", ") || "—"} · Период: {String(active.summary?.date_from ?? "—")} — {String(active.summary?.date_to ?? "—")} · Валюты: {strings(active.summary, "currencies").join(", ") || "—"} · Исключено: {excluded}</p></div>
            <label className="toggle"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />Я проверил preview и явно подтверждаю импорт</label>
            <button type="button" disabled={busy || !confirmed || active.status !== "ready"} onClick={() => void commit()}>Импортировать подтверждённые строки</button>
          </section>
        </div>
      ) : null}

      <section className="panel import-history">
        <div className="panel-heading"><div><span className="kicker">История</span><h2>Пакеты импорта</h2></div></div>
        <div className="table-scroll"><table><thead><tr><th>Файл</th><th>Дата</th><th>Строк</th><th>Статус</th><th>Действия</th></tr></thead><tbody>{batches.map((batch) => <tr key={batch.id}><td><button className="text-button" type="button" onClick={() => selectBatch(batch)}>{batch.filename}</button></td><td>{new Date(batch.created_at).toLocaleString("ru-RU")}</td><td>{number(batch.summary, "total") || number(batch.summary, "valid") + number(batch.summary, "invalid") + number(batch.summary, "duplicate")}</td><td><span className={`status-chip status-chip--${batch.status}`}>{batch.status}</span></td><td className="table-actions">{batch.status === "imported" ? <button className="text-button text-button--danger" type="button" disabled={busy} onClick={() => { setActive(batch); void run(async () => (await apiClient.post<{ batch: ImportBatch }>(`/api/v1/imports/${batch.id}/rollback`, { force: false })).batch); }}>Rollback</button> : null}{["mapping_required", "parsed", "validated", "ready"].includes(batch.status) ? <button className="text-button" type="button" onClick={() => void run(() => apiClient.post(`/api/v1/imports/${batch.id}/cancel`))}>Отменить</button> : null}</td></tr>)}</tbody></table></div>
      </section>
    </section>
  );
}
