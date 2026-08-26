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

## Development и production runtime

Compose намеренно имеет два режима. Базовый `docker-compose.yml` — development: backend
и sync-worker получают bind mount исходников, backend использует `uvicorn --reload`, а
frontend использует development image, source mount и cache volumes. Это обеспечивает
быстрый локальный цикл и не является production-конфигурацией.

Production всегда объединяет base с `compose.production.yml`. Override полностью заменяет
backend mounts утверждённым набором runtime data, удаляет mounts worker/frontend, задаёт
backend-команду без `--reload` и production target/`npm run start` для frontend. Поэтому
application code backend, worker и frontend поступает только из собранных immutable
images; изменение server checkout само по себе не меняет работающий код.

Итоговая топология проверяется
`backend/scripts/validate_compose_topology.py`: validator не печатает rendered environment
и прекращает deploy при утечке dev mount/command в production. Канонический server
override хранится в Git как `compose.production.yml`, а root-owned копия подключается
production wrapper из `/etc/finspace/compose.server.yml`.

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
- `month_close_controls`, `month_closures`, `month_close_revisions` — cumulative hard
  close, current preview и immutable confirmed history;
- `notification_settings` — адресная доставка.

Деньги — `NUMERIC(20,4)`, время — timezone-aware, идентификаторы — UUID. Soft delete и
optimistic locking сохранены. `transactions.import_batch_id` связывает импорт с
операциями и делает rollback точным.

## Hard Month Close и financial write boundary

`month_close_controls` содержит одну coordination row на workspace. Confirm и все
ledger-affecting writes сначала берут её `FOR UPDATE`; после domain locks mutation,
revision и audit фиксируются одной транзакцией. Записи с effective date не позднее
`closed_through` отклоняются как `MONTH_CLOSED`. Auto-reopen отсутствует.

Обычные операции создаются только через transaction domain service. Единственное
отдельное создание `FinancialTransaction` — атомарный import commit после полного
preflight. Google inbound, recurring и Telegram используют эти же границы; reconciliation
может перевести `confirmed` в `reconciled`, поскольку это не меняет финансовый эффект.
Подробный state machine, mutation matrix и snapshot contract описаны в
[Hard Month Close](month-close.md).

Month Close read model не смешивается с mutation path. Current preview остаётся в
`month_closures`, а подтверждённая история читается из append-only
`month_close_revisions`. As-closed report строится исключительно из revision snapshot;
отдельный comparison endpoint рассчитывает live current view и сопоставляет валюты,
остатки и категории без суммирования разных валют.

Issue policy формируется backend-ом в виде `blocker|warning|info`; frontend не угадывает
severity или роль пользователя. `capabilities` в response управляют видимостью действий,
но route dependencies остаются окончательной проверкой: viewer читает историю, editor
готовит период, owner подтверждает и открывает последний период повторно. Reconciliation
остаётся отдельным bounded context: Month Close только читает confirmed evidence и
сохраняет coverage в snapshot.

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
API, MFA и подтверждённого managed/offsite encrypted backup. Production n8n запущен и
healthy, остаётся локальным и видит только ограниченный Backend API; Telegram
настраивается отдельно. Будущие интеграции работают через API/outbox/staging, а не
напрямую пишут в PostgreSQL.
