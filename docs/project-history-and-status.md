# Finspace: история этапов и текущее состояние

Этот документ — постоянная передача контекста между владельцем проекта, разработчиками и
следующими AI-ассистентами. Он описывает не планы, а фактически реализованные возможности
и подтверждённое состояние на указанную дату. Секреты, токены, пароли, cookie, ключи,
Binding ID, идентификатор Google-книги и содержимое environment-файлов сюда не включаются.

## Как читать статусы

- **Реализовано** — код, миграции, тесты и документация присутствуют в Git.
- **Принято** — сценарий прошёл отдельную живую проверку.
- **Запущено** — сервис был обнаружен работающим на production-сервере при последней
  проверке.
- **Не активировано** — код существует, но рабочая настройка пользователя или запущенный
  сервис не подтверждены.

## Локальная контрольная точка 2026-08-23

Локальная интеграция нового интерфейса и финансовых доменов выполнена в ветке
`codex/finspace-ui-integration`. Созданы функциональные commits:

- `2b666cd` — routed App Shell, Dashboard, Accounts, Categories, Transactions, Reports и
  account reconciliation;
- `ec2e737` — hardened staged import;
- `743e171` — durable Apps Script queue и Google sync;
- `8e3cad0` — корректная idempotency recurring rules.

Эти commits **не отправлены и не развёрнуты на сервере**. Последняя подтверждённая
production-контрольная точка остаётся ниже; перед deploy обязательно сравнить server
`HEAD`, dirty status и migrations.

На локальном коде подтверждены:

- frontend: 72 теста, TypeScript, ESLint и production build;
- backend: 67 passed, 1 skipped; Ruff check, Ruff format по исходникам и Mypy;
- Apps Script queue harness: 10 passed;
- live E2E на отдельном синтетическом workspace: DNS failure и HTTP 503 сохраняют один
  event ID, retry применяет изменение ровно один раз, обе очереди завершаются нулём;
- account/category create → archive/delete → restore возвращают канонические DTO без
  ложного `INTERNAL_ERROR` даже при `expire_on_commit=True`;
- тестовая Google-книга, binding, triggers, объекты, контейнеры и локальные артефакты
  удалены; production-контур не изменялся.

Подробный обезличенный отчёт:
[Apps Script Bridge acceptance 2026-08-23](reports/apps-script-bridge-acceptance-2026-08-23.md).

## Production-контрольная точка 2026-08-06

| Параметр | Подтверждённое значение |
|---|---|
| Локальный репозиторий | `C:\Users\Nikit\Documents\Finans` |
| Репозиторий на сервере | `/opt/finspace` |
| Подключение к серверу | SSH alias `finspace` |
| Ветка | `main` |
| Локальный commit | `ec4abe7` |
| Commit на сервере | `ec4abe7` |
| Базовая версия этапов 1–6 | tag `local-v0.6.0`, commit `cfc8276` |
| Production frontend | `https://terontyn-pc.tailfcdf00.ts.net/`, доступен внутри tailnet |
| Публичный backend для Apps Script | `https://terontyn-pc.tailfcdf00.ts.net:8443` |
| Основной Google provider | `apps_script_bridge` |
| Источник финансовой истины | PostgreSQL |

Перед любой новой работой всё равно нужно повторно выполнить `git status`, `git rev-parse
--short HEAD`, проверку контейнеров и health: этот снимок со временем устареет.

### Запущенные сервисы

На сервере подтверждены:

- `frontend` — production Next.js (`next start`), healthy, host `127.0.0.1:3000`;
- `backend` — FastAPI, healthy, host `127.0.0.1:8000`;
- `sync-worker` — запущен; при основном provider Apps Script Bridge не забирает Bridge
  events и сохранён для optional Google OAuth provider;
- `postgres` — PostgreSQL 17, healthy, host `127.0.0.1:5432`;
- `redis` — Redis 8, healthy, host `127.0.0.1:6379`.

На момент проверки **не запущены** `n8n` и `adminer`. Это нормальное состояние, пока
автоматизации/Telegram не настроены и доступ к Adminer не нужен.

### Сетевой доступ

- Tailscale Serve публикует frontend на HTTPS 443 только внутри tailnet и направляет
  запросы на `127.0.0.1:3000`.
- Tailscale Funnel публично публикует только backend HTTPS 8443 и направляет запросы на
  `127.0.0.1:8000`. Публичность нужна Apps Script, который исполняется в инфраструктуре
  Google и не находится в tailnet.
- Браузер Finspace не обращается к 8443: он использует same-origin `/api/*`, а Next.js
  проксирует запросы к `http://backend:8000` внутри Docker network.
- PostgreSQL, Redis, Adminer и n8n через Serve/Funnel не публикуются.

Домен `terontyn.site` делегирован Cloudflare. Named Cloudflare Tunnel и DNS route
`api.terontyn.site` сохранены как резервная конфигурация, но connector `cloudflared`
остановлен и отключён (`inactive`, `disabled`) из-за нестабильного соединения. Пока он
выключен, `api.terontyn.site` не является рабочим backend URL.

### Google Sheets

Текущая Apps Script Bridge-настройка принята:

- backend config check завершался всеми `PASS` без вывода секретов;
- создан binding, одноразовый secret сохранён только владельцем;
- container-bound Apps Script v1 установлен в пользовательскую Google-книгу;
- `setupFinspace()` создал шаблон;
- `configureConnection()` зарегистрировал книгу со статусом `registered`;
- installable triggers установлены с интервалом 5 минут;
- initial pull завершился без ошибок;
- Finspace показывает `двусторонняя синхронизация`, зарегистрированную таблицу и активный
  heartbeat;
- Apps Script отдельно получил HTTP 200 от публичного backend health через Tailscale
  Funnel.

Значения binding secret, service key, spreadsheet ID и Document Properties нельзя
переносить в Git, задачи, логи или сообщения ассистенту.

## Выполненные этапы

### Этап 1 — локальный каркас

Статус: **реализовано**.

- Создан Docker Compose-каркас для Next.js, FastAPI, PostgreSQL, Redis и Adminer.
- Порты development-сервисов привязаны к loopback.
- Добавлены health/readiness, структурированные ошибки и request ID.
- Схема управляется Alembic; PostgreSQL выбран источником истины.
- Добавлены seed, базовые тесты, скрипты запуска/логов/остановки и безопасный reset с
  подтверждением.

Архитектурное решение: [ADR 0001](decisions/0001-local-docker-architecture.md).

### Этап 2 — финансовое ядро

Статус: **реализовано**.

- Добавлены workspaces, счета, категории, операции и splits.
- Деньги хранятся как `NUMERIC(20,4)`/Decimal, идентификаторы — UUID, время — timezone-aware.
- Реализованы soft delete, optimistic locking, ограничения и workspace isolation.
- Изменение финансовой сущности и audit фиксируются одной транзакцией.
- Реализованы остатки, summary и основные frontend-экраны.

Архитектурное решение: [ADR 0002](decisions/0002-financial-core.md).

### Этап 3 — аутентификация, backup и импорт

Статус: **реализовано**.

- Пароли хэшируются Argon2id.
- Access JWT хранится frontend только в памяти; refresh token ротируется через
  `HttpOnly` cookie, а в БД хранится только hash.
- Реализованы logout/logout-all, обнаружение повторного refresh token, rate limit и роли
  viewer/editor/owner.
- Backup создаётся `pg_dump` в custom format с SHA-256 manifest.
- Проверка backup восстанавливает dump в отдельную временную БД и не затирает рабочую.
- CSV/XLSX проходят staging: upload → mapping → validation → commit; есть дедупликация и
  безопасный rollback.

Подробности: [ADR 0003](decisions/0003-auth-backup-import.md),
[backup](backup-and-restore.md), [import](import.md).

### Этап 4 — Google Sheets и безопасная синхронизация

Статус: **реализовано**.

- Добавлены transactional outbox/inbox, HMAC webhook, replay protection, retries, lease и
  ACK.
- Добавлены row version/hash, конфликты, reconciliation и разрешения
  `keep_database`/`keep_sheet`/`manual_merge`.
- PostgreSQL остаётся источником истины; удаление строки Sheets не удаляет запись в БД.
- Исходный Google OAuth/Sheets API adapter сохранён как optional provider и по умолчанию
  выключен.

Подробности: [ADR 0004](decisions/0004-google-sheets-sync.md),
[синхронизация](synchronization.md), [конфликты](sync-conflicts.md).

### Этап 4.5 — защита тестовой среды

Статус: **реализовано и принято**.

- Test runner создаёт уникальную БД с обязательным test marker.
- Pytest и migration cycle прекращаются до изменения данных, если окружение похоже на
  development/production.
- Cleanup живой Google-приёмки ограничен точным acceptance run ID и workspace UUID.
- Отчёты не содержат credentials и полных финансовых payload.

Подробности: [ADR 0005](decisions/0005-test-isolation-and-live-google-acceptance.md).

### Корректировка этапа 4 — Apps Script Bridge

Статус: **реализовано и является основным production flow**.

- Google Cloud Console, OAuth Client ID, Drive API и Sheets API не требуются.
- Пользователь вручную создаёт книгу и устанавливает пакет Apps Script v1.
- Binding secret показывается один раз; backend хранит derived hash.
- Apps Script выполняет подписанные `register`, `push`, `pull`, `ack`, `heartbeat` и
  reconciliation-запросы.
- В книгу не записываются JWT, пароль, Google token или binding secret.

Подробности: [ADR 0006](decisions/0006-apps-script-bridge.md),
[Apps Script Bridge](apps-script-bridge.md),
[установка без Google Cloud](google-without-cloud-console.md).

### Этап 5 — живая приёмка Apps Script Bridge

Статус: **полностью принят 2026-07-22**.

Проверены template, initial export, оба направления, идемпотентность, HMAC/replay,
pause/resume, lease, три варианта разрешения конфликтов, полная сверка, недоступность
backend, trigger lifecycle, backup/restore и точечный cleanup. Все 25 обязательных
evidence-пунктов получили `passed`; применялись только искусственные данные.

Полный обезличенный отчёт: [Apps Script Bridge acceptance](reports/apps-script-bridge-acceptance-2026-07-22.md).

### Этап 6 — n8n, Telegram и автоматизации

Статус: **реализовано в коде; на текущем сервере не активировано**.

- Добавлены workspace-scoped service accounts и hash-only ServiceKey.
- n8n изолирован в отдельной сети и не имеет прямого доступа к PostgreSQL/Redis.
- Добавлены recurring rules, Telegram intents с подтверждением, weekly report,
  uncategorized reminder, month-close reminder и backup-health workflow.
- Финансовые правила и audit остаются в Backend; n8n — только планировщик и транспорт.
- Workflow защищены idempotency key; потенциально опасные действия требуют подтверждения.

Для активации нужны отдельный `N8N_ENCRYPTION_KEY`, owner setup n8n, service account,
ServiceKey credential и Telegram bot credential. До этого нельзя считать Telegram и
автоматические расписания работающими.

Подробности: [ADR 0007](decisions/0007-n8n-automation-boundary.md), [n8n](n8n.md),
[Telegram](telegram.md).

## Исправления после этапа 6

После baseline `local-v0.6.0` выполнены отдельные production-исправления:

- восстановление auth-сессии завершается безопасно при отсутствии/повреждении данных,
  ошибке или зависшем fetch; timeout — 10 секунд, состояние React не обновляется после
  unmount;
- неавторизованный пользователь направляется на `/login`, а loading-экран показывается
  только во время реального восстановления;
- frontend переведён с `next dev` на multi-stage production image и `next start` от
  непривилегированного пользователя;
- браузерный API переведён на same-origin proxy;
- URL builder нормализует пустой base, `/` и абсолютный URL без `//api/...`;
- исправлен фактический вызов browser fetch;
- frontend переработан в актуальную тёмную тему, шрифты хранятся локально.

История соответствующих commits: `4ce6fdc` → `0eafc5d` → `49d928e` → `c0a8dd4` →
`de5da7d` → `91b70dc` → `6b1e920` → `8dc8de4` → `682f58f` → `5c78676` → `ec4abe7`.

### Локальная UI/domain integration 2026-08-23

Статус: **реализовано и проверено локально; не развёрнуто**.

- Глобальный screen state заменён на Next.js routes и единый responsive App Shell с
  light/dark themes, sidebar/mobile navigation и command palette.
- Dashboard, Accounts, Categories и Transactions используют только реальные API; mock
  финансовые показатели удалены. Неподдерживаемые Budget, Payees, Rules и Goals честно
  отмечены как требующие отдельного API.
- Добавлены account detail и bounded reconciliation: preview/confirm/history, точный
  Decimal, optimistic locking и запрет скрытой adjustment-операции.
- Добавлен backend financial reports API и production Reports screen; transfer не
  смешивается с income/expense cash flow.
- Staged import получил явные состояния review, строгую server-side validation,
  duplicate semantics и атомарный commit/rollback.
- Apps Script queue теперь сохраняет event ID в note строки, восстанавливается по
  `DIRTY`/`PENDING`, не удаляет событие без валидного подтверждения и безопасно повторяет
  DNS/timeout/HTTP 5xx.
- Account/category mutation routes формируют response DTO до commit, поэтому commit
  expiration ORM-объекта не превращается в ложный HTTP 500 после успешной записи.

### Hard Month Close Stage A/B 2026-08-25

Статус: **реализовано и проверяется локально; не развёрнуто**.

- Добавлены workspace control row, cumulative `closed_through` и immutable confirmed
  revisions с deterministic financial fingerprint.
- Month Close переведён на последовательный hard close без auto-reopen; manual reopen
  доступен только owner и только для последнего закрытого месяца.
- Confirm и reopen получили обязательную durable idempotency, prepare/confirm stale
  detection и единый lock order.
- Central guard подключён к transaction lifecycle, историческим account mutations,
  import commit/rollback, Google inbound/conflict resolution, recurring и Telegram.
- Account reconciliation после close остаётся разрешённым и не меняет fingerprint.

Контракт и mutation matrix: [Hard Month Close](month-close.md).

## Нереализованное или неподтверждённое

- n8n и Telegram не активированы на production-сервере.
- Не подтверждена регулярная внешняя зашифрованная копия backup вне сервера.
- MFA отсутствует.
- Банковские интеграции отсутствуют. На будущее рассмотрен только официальный BCS Trade
  API с read-only token; для личного СберБанк Онлайн планируется безопасный CSV/XLSX
  import, а не хранение банковского логина/пароля.
- Cloudflare Tunnel сохранён только как rollback-конфигурация и сейчас не работает.
- Tailscale Funnel делает backend доступным из интернета; требуется сохранять минимальную
  поверхность API, HMAC-защиту Apps Script и не публиковать внутренние сервисы.

## Главные инварианты проекта

1. PostgreSQL — единственный источник финансовой истины.
2. Google Sheets, n8n и будущие банковские connectors работают только через Backend
   API/outbox/inbox/staging, никогда через прямой SQL.
3. Финансовое изменение, audit и outbox фиксируются атомарно.
4. Тесты никогда не используют production/development DB.
5. Секреты не входят в Git, frontend build args, workflow JSON, Apps Script cells, логи и
   отчёты.
6. Никакой destructive command (`down -v`, reset, удаление volumes/DB) не выполняется без
   отдельного точного подтверждения владельца.
7. Production frontend запускается только через `compose.production.yml`/server override
   и `next start`, без bind mounts `node_modules`/`.next`.
8. `month_close_controls.closed_through` является hard accounting cutoff; никакая
   интеграция или service account не может обойти central guard или автоматически reopen.

Практические команды и действия находятся в [эксплуатационной инструкции](operations-runbook.md).
