"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  importMappingTargets,
  importStatusLabel,
  importStep,
  mappingMissing,
  rowErrors,
  rowSource,
} from "@/components/screens/import-workflow";
import { apiClient } from "@/lib/api-client";
import type { ImportBatch, ImportRow, Paged } from "@/types/finance";

interface ImportScreenProps {
  onError: (error: unknown) => void;
}

interface ImportResult {
  batch: ImportBatch;
  affected_transactions: number;
}

function number(summary: Record<string, unknown> | null, key: string): number {
  const value = summary?.[key];
  return typeof value === "number" ? value : 0;
}

function strings(summary: Record<string, unknown> | null, key: string): string[] {
  const value = summary?.[key];
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function formatSize(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(1)} КБ`
    : `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

function importSummaryHeading(batch: ImportBatch): string {
  const count =
    number(batch.summary, "affected_transactions") || number(batch.summary, "valid");
  if (batch.status === "imported") return `Импортировано операций: ${count}`;
  if (batch.status === "rolled_back") return "Импорт отменён откатом";
  if (batch.status === "cancelled") return "Batch отменён";
  return `Будет создано операций: ${count}`;
}

export function ImportScreen({ onError }: ImportScreenProps) {
  const [batches, setBatches] = useState<ImportBatch[]>([]);
  const [active, setActive] = useState<ImportBatch | null>(null);
  const [rows, setRows] = useState<ImportRow[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [forceDuplicate, setForceDuplicate] = useState(false);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [locale, setLocale] = useState<"ru-RU" | "en-US">("ru-RU");
  const [rowFilter, setRowFilter] = useState("all");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const generation = useRef(0);

  const loadBatches = useCallback(async () => {
    const page = await apiClient.get<Paged<ImportBatch>>("/api/v1/imports?limit=50");
    setBatches(page.items);
  }, []);

  const loadRows = useCallback(async (batchId: string, filter: string, token = generation.current) => {
    const query = filter === "all"
      ? ""
      : filter === "errors"
        ? "?has_errors=true"
        : filter === "duplicate"
          ? "?duplicate=true"
          : `?status=${filter}`;
    const page = await apiClient.get<Paged<ImportRow>>(`/api/v1/imports/${batchId}/rows${query}`);
    if (token !== generation.current) return;
    setRows(page.items);
  }, []);

  useEffect(() => {
    let mounted = true;
    queueMicrotask(() => {
      void loadBatches()
        .catch(onError)
        .finally(() => {
          if (mounted) setLoading(false);
        });
    });
    return () => { mounted = false; };
  }, [loadBatches, onError]);

  const columns = useMemo(
    () => strings(active?.summary ?? null, "source_columns"),
    [active],
  );
  const missingMapping = useMemo(() => mappingMissing(mapping), [mapping]);
  const currentStep = importStep(active);
  const mappingEditable = Boolean(
    active && ["mapping_required", "parsed", "validated", "ready"].includes(active.status),
  );
  const duplicateOverrideAllowed = Boolean(
    active && ["validated", "ready"].includes(active.status),
  );

  function selectBatch(batch: ImportBatch): void {
    const token = generation.current + 1;
    generation.current = token;
    setActive(batch);
    setRows([]);
    setMapping(batch.mapping?.fields ?? {});
    setLocale(batch.mapping?.locale === "en-US" ? "en-US" : "ru-RU");
    setConfirmed(false);
    setResult(null);
    setBusy(false);
    void loadRows(batch.id, rowFilter, token).catch((error) => {
      if (token === generation.current) onError(error);
    });
  }

  async function run(action: () => Promise<ImportBatch>): Promise<ImportBatch | null> {
    const token = generation.current;
    setBusy(true);
    setResult(null);
    try {
      const next = await action();
      if (token !== generation.current) return null;
      setActive(next);
      await Promise.all([loadBatches(), loadRows(next.id, rowFilter, token)]);
      return next;
    } catch (error) {
      if (token === generation.current) onError(error);
      return null;
    } finally {
      if (token === generation.current) setBusy(false);
    }
  }

  async function upload(): Promise<void> {
    if (!file) return;
    const token = generation.current + 1;
    generation.current = token;
    setBusy(true);
    setResult(null);
    try {
      const form = new FormData();
      form.set("file", file);
      form.set("force_duplicate", String(forceDuplicate));
      const batch = await apiClient.upload<ImportBatch>("/api/v1/imports", form);
      if (token !== generation.current) return;
      setFile(null);
      setForceDuplicate(false);
      setActive(batch);
      setRows([]);
      setMapping(batch.mapping?.fields ?? {});
      setLocale(batch.mapping?.locale === "en-US" ? "en-US" : "ru-RU");
      setConfirmed(false);
      await Promise.all([loadBatches(), loadRows(batch.id, rowFilter, token)]);
    } catch (error) {
      if (token === generation.current) onError(error);
    } finally {
      if (token === generation.current) setBusy(false);
    }
  }

  async function saveMapping(): Promise<void> {
    if (!active || missingMapping.length) return;
    await run(() => apiClient.put(`/api/v1/imports/${active.id}/mapping`, {
      locale,
      mapping: Object.fromEntries(Object.entries(mapping).filter(([, value]) => value)),
    }));
  }

  async function commit(): Promise<void> {
    if (!active || !confirmed) return;
    const batchId = active.id;
    const token = generation.current;
    setBusy(true);
    try {
      const response = await apiClient.post<ImportResult>(
        `/api/v1/imports/${batchId}/commit`,
        { confirm: true },
        { "X-Idempotency-Key": `import-${batchId}` },
      );
      if (token !== generation.current) return;
      setActive(response.batch);
      setResult(response);
      setConfirmed(false);
      await Promise.all([loadBatches(), loadRows(batchId, rowFilter, token)]);
    } catch (error) {
      if (token === generation.current) onError(error);
    } finally {
      if (token === generation.current) setBusy(false);
    }
  }

  async function importDuplicateAsNew(row: ImportRow): Promise<void> {
    if (!active) return;
    const batchId = active.id;
    const token = generation.current;
    setBusy(true);
    try {
      await apiClient.patch(`/api/v1/imports/${batchId}/rows/${row.id}`, {
        import_as_new: true,
      });
      const refreshed = await apiClient.get<ImportBatch>(`/api/v1/imports/${batchId}`);
      if (token !== generation.current) return;
      setActive(refreshed);
      await loadRows(batchId, rowFilter, token);
    } catch (error) {
      if (token === generation.current) onError(error);
    } finally {
      if (token === generation.current) setBusy(false);
    }
  }

  const excluded = active
    ? number(active.summary, "invalid")
      + number(active.summary, "duplicate")
      + number(active.summary, "skipped")
    : 0;

  return (
    <section>
      <header className="screen-header">
        <div>
          <span className="kicker">Безопасный staging</span>
          <h1>Импорт операций</h1>
          <p>CSV и XLSX проходят серверное сопоставление и проверку до записи в PostgreSQL.</p>
        </div>
      </header>

      <ol aria-label="Этапы импорта" className="import-steps">
        {["Файл", "Сопоставление", "Проверка", "Импорт"].map((label, index) => (
          <li className={currentStep >= index + 1 ? "is-active" : ""} key={label}>
            <span>{index + 1}</span>{label}
          </li>
        ))}
      </ol>

      <div className="panel import-upload">
        <div>
          <strong>1. Выберите файл</strong>
          <p>Поддерживаются CSV (UTF-8/Windows-1251) и безопасное чтение XLSX. До финального шага операции не создаются.</p>
        </div>
        <input
          aria-label="Файл импорта"
          type="file"
          accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        {file ? <span className="file-summary">{file.name} · {formatSize(file.size)}</span> : null}
        <label className="toggle">
          <input type="checkbox" checked={forceDuplicate} onChange={(event) => setForceDuplicate(event.target.checked)} />
          Создать новый batch, даже если такой файл уже загружался
        </label>
        <button type="button" disabled={!file || busy} onClick={() => void upload()}>Загрузить в staging</button>
      </div>

      {loading ? <div className="panel import-loading">Загружаем историю импорта…</div> : null}

      {active ? (
        <div className="import-workflow">
          <section className="panel">
            <div className="panel-heading">
              <div>
                <span className="kicker">Batch {active.id.slice(0, 8)}</span>
                <h2>{active.filename}</h2>
                <p>{formatSize(active.file_size)} · {active.file_type.toUpperCase()} · {active.detected_format ?? "формат не определён"}</p>
              </div>
              <span className={`status-chip status-chip--${active.status}`}>{importStatusLabel(active.status)}</span>
            </div>

            <h3>2. Сопоставление колонок</h3>
            <p className="section-copy">Обязательны дата, счёт и сумма. Для единой колонки суммы также нужен тип операции.</p>
            <div className="mapping-grid">
              {importMappingTargets.map(([target, label]) => (
                <label key={target}>
                  {label}
                  <select disabled={!mappingEditable || busy} value={mapping[target] ?? ""} onChange={(event) => setMapping((current) => ({ ...current, [target]: event.target.value }))}>
                    <option value="">Не импортировать</option>
                    {columns.map((column) => <option key={column} value={column}>{column}</option>)}
                  </select>
                </label>
              ))}
              <label>
                Формат даты
                <select disabled={!mappingEditable || busy} value={locale} onChange={(event) => setLocale(event.target.value as "ru-RU" | "en-US")}>
                  <option value="ru-RU">ru-RU — день/месяц</option>
                  <option value="en-US">en-US — месяц/день</option>
                </select>
              </label>
            </div>
            {missingMapping.length ? <p className="field-hint field-hint--warning">Укажите: {missingMapping.join(", ")}.</p> : null}
            <div className="workflow-actions">
              <button className="secondary-button" type="button" disabled={busy || !mappingEditable || Boolean(missingMapping.length)} onClick={() => void saveMapping()}>Сохранить сопоставление</button>
              <button type="button" disabled={busy || !["parsed", "validated", "ready"].includes(active.status)} onClick={() => void run(() => apiClient.post(`/api/v1/imports/${active.id}/validate`))}>Проверить строки</button>
            </div>
          </section>

          <section className="panel">
            <div className="panel-heading"><div><span className="kicker">3. Проверка</span><h2>Что будет импортировано</h2></div></div>
            <div className="import-metrics">
              <div><span>Готовы</span><strong>{number(active.summary, "valid")}</strong></div>
              <div><span>Ошибки</span><strong>{number(active.summary, "invalid")}</strong></div>
              <div><span>Дубликаты</span><strong>{number(active.summary, "duplicate")}</strong></div>
              <div><span>Пропущены</span><strong>{number(active.summary, "skipped")}</strong></div>
            </div>
            <div className="filters import-filters">
              <select aria-label="Фильтр строк" value={rowFilter} onChange={(event) => {
                const value = event.target.value;
                const token = generation.current;
                setRowFilter(value);
                void loadRows(active.id, value, token).catch((error) => {
                  if (token === generation.current) onError(error);
                });
              }}>
                <option value="all">Все строки</option>
                <option value="valid">Готовы</option>
                <option value="errors">С ошибками</option>
                <option value="duplicate">Дубликаты</option>
                <option value="skipped">Пропущены</option>
              </select>
            </div>
            <div className="table-scroll import-row-table"><table><thead><tr><th>Строка</th><th>Статус</th><th>Исходные данные</th><th>Причина</th><th>Решение</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td>{row.source_sheet ? `${row.source_sheet}:` : ""}{row.source_row_number}</td><td><span className={`status-chip status-chip--${row.status}`}>{importStatusLabel(row.status)}</span></td><td>{rowSource(row)}</td><td>{rowErrors(row)}</td><td>{row.status === "duplicate" && duplicateOverrideAllowed ? <button className="text-button" type="button" disabled={busy} onClick={() => void importDuplicateAsNew(row)}>Импортировать как новую</button> : "—"}</td></tr>)}</tbody></table></div>
            <div className="import-row-mobile-list">{rows.map((row) => <article key={row.id}><header><strong>Строка {row.source_sheet ? `${row.source_sheet}:` : ""}{row.source_row_number}</strong><span className={`status-chip status-chip--${row.status}`}>{importStatusLabel(row.status)}</span></header><p>{rowSource(row)}</p><small>{rowErrors(row)}</small>{row.status === "duplicate" && duplicateOverrideAllowed ? <button className="text-button" type="button" disabled={busy} onClick={() => void importDuplicateAsNew(row)}>Импортировать как новую</button> : null}</article>)}</div>
          </section>

          <section className="panel confirmation-panel">
            <div>
              <span className="kicker">4. Импорт</span>
              <h2>{importSummaryHeading(active)}</h2>
              <p>Счета: {strings(active.summary, "accounts").join(", ") || "—"} · Период: {String(active.summary?.date_from ?? "—")} — {String(active.summary?.date_to ?? "—")} · Валюты учитываются отдельно: {strings(active.summary, "currencies").join(", ") || "—"} · Исключено: {excluded}</p>
            </div>
            {["parsed", "validated", "ready"].includes(active.status) ? <>
              <label className="toggle"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />Я проверил строки</label>
              <button type="button" disabled={busy || !confirmed || active.status !== "ready"} onClick={() => void commit()}>Импортировать batch</button>
            </> : <span className="field-hint">Batch завершён; повторный commit недоступен.</span>}
          </section>

          {result ? (
            <div className="notice notice--success import-success" role="status">
              <strong>Импорт завершён</strong>
              <span>
                Создано операций: {result.affected_transactions}. Без категории:{" "}
                {number(result.batch.summary, "uncategorized_at_commit")}. Исключено: {excluded}.
              </span>
              <Link href="/transactions">Открыть операции</Link>
              {result.batch.status === "imported" &&
              number(result.batch.summary, "review_candidates_at_commit") > 0 ? (
                <Link href={`/rules/review?import_batch_id=${result.batch.id}`}>
                  Проверить категоризацию этого импорта
                </Link>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      <section className="panel import-history">
        <div className="panel-heading"><div><span className="kicker">История</span><h2>Пакеты импорта</h2></div></div>
        {!loading && !batches.length ? <div className="empty-state">Импортов пока нет.</div> : null}
        {batches.length ? <div className="table-scroll"><table><thead><tr><th>Файл</th><th>Дата</th><th>Строк</th><th>Статус</th><th>Действия</th></tr></thead><tbody>{batches.map((batch) => <tr key={batch.id}><td><button className="text-button" type="button" onClick={() => selectBatch(batch)}>{batch.filename}</button></td><td>{new Date(batch.created_at).toLocaleString("ru-RU")}</td><td>{number(batch.summary, "total") || number(batch.summary, "valid") + number(batch.summary, "invalid") + number(batch.summary, "duplicate") + number(batch.summary, "skipped")}</td><td><span className={`status-chip status-chip--${batch.status}`}>{importStatusLabel(batch.status)}</span></td><td className="table-actions">{batch.status === "imported" ? <button className="text-button text-button--danger" type="button" disabled={busy} onClick={() => { if (window.confirm("Отменить этот импорт? Изменённые после импорта операции требуют отдельного принудительного решения.")) { selectBatch(batch); void run(async () => (await apiClient.post<ImportResult>(`/api/v1/imports/${batch.id}/rollback`, { force: false })).batch); } }}>Откатить</button> : null}{["mapping_required", "parsed", "validated", "ready"].includes(batch.status) ? <button className="text-button" type="button" onClick={() => void run(() => apiClient.post(`/api/v1/imports/${batch.id}/cancel`))}>Отменить</button> : null}{batch.status === "imported" && number(batch.summary, "review_candidates_at_commit") > 0 ? <Link className="text-button" href={`/rules/review?import_batch_id=${batch.id}`}>Проверить категоризацию</Link> : null}</td></tr>)}</tbody></table></div> : null}
      </section>
    </section>
  );
}
