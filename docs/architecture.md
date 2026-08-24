# Архитектура

## Контейнеры и потоки

```text
Tailnet browser -> Tailscale Serve :443 -> Next.js :3000 -> /api/* -> FastAPI :8000
                                                                   |-> PostgreSQL :5432
                                                                   `-> Redis :6379
Google Apps Script -> Tailscale Funnel :8443 -> FastAPI HMAC Bridge endpoints
backup service -> pg_dump/pg_restore -> ./backups -> временная restore-БД
FastAPI upload -> ./data/imports -> import_batches/import_rows -> transactions
FastAPI -> PostgreSQL + sync_outbox <- Apps Script pull/ACK
Google Sheets -> Apps Script HMAC push -> sync_inbox -> validation/conflicts -> PostgreSQL
optional sync-worker -> Google Sheets API (только provider google_oauth)
n8n schedule/Telegram long polling -> ServiceKey Backend API -> domain services -> audit/outbox
```

PostgreSQL остаётся источником финансовой истины. Backup service запускается только по
profile `tools` и не останавливает основную БД. Каталог импорта не публикуется Next.js.
Google API никогда не вызывается внутри финансовой DB-транзакции.
n8n находится в отдельной сети только с Backend; PostgreSQL и Redis остаются только в
`finspace`. Workflow не являются источником финансовой истины и не выполняют SQL.

## Backend-слои

```text
API routes -> auth/role dependencies -> domain services -> repositories -> PostgreSQL
                        |                    |                 |
                        |                    +-> audit         +-> workspace filter
                        `-> JWT/session + Redis limit
```

Routes задают HTTP-контракт. Сервисы содержат инварианты и управляют транзакцией.
Repositories финансового ядра явно принимают `workspace_id`; чужой UUID выглядит как
отсутствующий. Request ID проходит через JSON-лог, единый error envelope и audit.

## Идентичность и авторизация

`users` содержит Argon2id hash и состояние login lock. `auth_sessions` хранит только
hash refresh token и метаданные клиента в виде hash. Access JWT определяет пользователя,
после чего dependency проверяет membership выбранного workspace и роль. Заголовок
workspace не является источником доверия.

Frontend держит access token в памяти. При загрузке или первом 401 выполняется один
refresh; успешный refresh повторяет исходный запрос один раз. Неуспех очищает память и
переводит на login.

## Финансовая и staging-модели

- `users`, `workspaces`, `workspace_members` — идентичность, изоляция и роли;
- `accounts`, `categories`, `transactions`, `transaction_splits` — финансовое ядро;
- `import_batches`, `import_rows` — staging, validation и provenance;
- `auth_sessions` — ротируемые refresh-сессии;
- `audit_log` — безопасные before/after snapshots и operational events;
- `system_metadata` — версия и проверочный read-only объект.
- `google_connections`, `google_oauth_flows`, `google_sheet_bindings` — OAuth и книга;
- `sync_outbox`, `sync_inbox`, `sync_conflicts`, `sync_runs` — доставка и сверка.
- `service_accounts`, `service_api_keys`, `automation_runs` — scoped machine auth и
  идемпотентный журнал запусков;
- `recurring_rules`, `recurring_rule_executions` — расписания и их уникальные исполнения;
- `telegram_links`, `telegram_link_codes`, `telegram_intents` — безопасная привязка и
  подтверждаемые команды;
- `month_closures`, `notification_settings` — контроль периодов и адресная доставка.

Деньги — `NUMERIC(20,4)`, время — timezone-aware, идентификаторы — UUID. Soft delete и
optimistic locking сохранены. `transactions.import_batch_id` связывает импорт с
операциями и делает rollback точным.

## Атомарность

Регистрация создаёт user/workspace/member/session одной транзакцией. Финансовое изменение,
audit и outbox фиксируются вместе. Commit импорта создаёт только valid rows и их связи одной
DB-транзакцией; частичный импорт не остаётся. Rollback не касается ручных операций.

Backup не является частью бизнес-транзакции: целостность доказывается `pg_restore --list`,
SHA-256 и пробным восстановлением в отдельную БД.

## Текущие границы

Google Sheets поддерживает одну зарегистрированную книгу на workspace; произвольные и
несколько книг не синхронизируются. Self-hosted production frontend доступен только внутри
tailnet, а публичный Funnel 8443 используется Apps Script Bridge. Нет OCR, банковских/ИИ
API, MFA и подтверждённого managed/offsite encrypted backup. Код n8n и Telegram реализован,
но их production-активация выполняется отдельно; n8n должен оставаться локальным и видеть
только ограниченный Backend API. Будущие интеграции работают через API/outbox/staging, а
не напрямую пишут в PostgreSQL.
