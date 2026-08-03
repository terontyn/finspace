# Конфликты и полная сверка

Устаревшая Sheet version не применяется. `sync_conflicts` сохраняет версии, безопасные
payload snapshots и список полей; token, secret и traceback туда не попадают.

В UI доступны решения:

- `keep_database` — повторно поставить canonical PostgreSQL row в outbox;
- `keep_sheet` — заново валидировать видимые Sheet changes против текущей версии;
- `manual_merge` — передать явно объединённые поля и повторить ту же validation.

Reconciliation читает три primary sheets и сравнивает UUID, version, row hash, deleted state,
duplicates, unknown и missing rows. Missing row безопасно возвращается из PostgreSQL через
outbox. Unknown/duplicate/technical mismatch не принимаются автоматически. Отсутствие строки
никогда не означает soft/physical delete PostgreSQL.

Каждая сверка создаёт `sync_runs` и audit `sheet.reconcile`. Результаты: `matched`,
`database_newer`, `sheet_newer`, `conflict`, `missing_in_sheet`, `unknown_in_sheet`,
`duplicate_in_sheet`, `invalid`.
