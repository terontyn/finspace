# Финпространство

Локальное приложение для личных и семейных финансов. Основной Google provider — Apps
Script Bridge без Google Cloud OAuth: пользователь вручную создаёт таблицу, а backend
синхронизирует её через HMAC pull/ACK/push поверх transactional outbox/inbox.

> [!WARNING]
> Используйте только тестовые данные. Проект не имеет публичного TLS, шифрования backup
> и удалённой копии, поэтому пока не предназначен для реальных финансовых данных.

## Сервисы

- Next.js UI — <http://localhost:3000>;
- FastAPI и Swagger — <http://localhost:8000/docs>;
- PostgreSQL — `127.0.0.1:5432`;
- Redis — `127.0.0.1:6379`;
- Adminer — <http://localhost:8080>;
- n8n — <http://localhost:5678>, отдельная изолированная сеть без PostgreSQL/Redis;
- `backup` — вызываемый Compose service с `pg_dump`/`pg_restore`.
- `sync-worker` — optional outbox consumer только для provider `google_oauth`.

Host-порты привязаны к loopback. Браузер обращается к same-origin prefix `/local-api`,
который Next.js проксирует в backend.

## Первый запуск

```powershell
Copy-Item .env.example .env
# Замените POSTGRES_PASSWORD, JWT_SECRET_KEY и N8N_ENCRYPTION_KEY в .env.
docker compose up -d --build
docker compose exec backend alembic upgrade head
docker compose exec backend python -m app.db.seed
docker compose ps
```

Откройте <http://localhost:3000/register>, создайте пользователя и первое пространство,
либо на странице входа подготовьте development-пользователя и задайте ему пароль.
Bootstrap не обходит аутентификацию и недоступен вне development.

Текущая вершина миграций — `0006_automations_telegram`. Миграции `0001`–`0005`
сохранены без изменений.

## Автоматизации и Telegram

n8n запускается локально и обращается только к Backend Automation API. Он не получает
доступ к PostgreSQL, Redis, пользовательским JWT или Google secrets. Регулярные операции,
Telegram intents, отчёты, закрытие месяца и backup health проверяются и фиксируются
Backend с audit, idempotency и transactional outbox.

```powershell
make n8n-up
make n8n-status
make n8n-import
```

До запуска задайте настоящий случайный `N8N_ENCRYPTION_KEY`, создайте n8n service account
в разделе **Автоматизации** и сохраните показанный один раз ServiceKey в n8n credential.
Telegram bot token также хранится только в n8n credentials. Пошаговая настройка:
[docs/n8n.md](docs/n8n.md) и [docs/telegram.md](docs/telegram.md).

## Google Sheets

Google Cloud credentials не нужны. Настройте `PUBLIC_BACKEND_URL`, создайте binding в
разделе **Google Sheets**, вручную создайте пустую таблицу и установите
[Apps Script](docs/google-apps-script.md). Пошагово: [Google без Cloud
Console](docs/google-without-cloud-console.md). Старый OAuth/Sheets API provider сохранён,
но по умолчанию выключен. PostgreSQL остаётся источником финансовой истины.

```powershell
make sync-worker
make google-test
make sync-test
make reconciliation-test
make apps-script-package
```

## Безопасность и роли

- пароль длиной 10–128 символов хранится только как Argon2id hash;
- короткоживущий HS256 access JWT хранится frontend только в памяти;
- одноразовый refresh token ротируется через `HttpOnly`, `SameSite=Lax` cookie;
- повтор старого refresh token отзывает активные сессии пользователя;
- Redis ограничивает вход по нормализованному email и hash IP;
- viewer читает, editor меняет финансовые данные и импортирует, owner управляет
  участниками;
- `X-Workspace-ID` только выбирает workspace, membership всегда проверяет backend;
- `X-User-ID` по умолчанию не принимается; dev headers требуют явного feature flag.

Подробности: [docs/security.md](docs/security.md).

## Резервные копии

```powershell
make backup
make backup-verify
make restore-test
make backup-cleanup

# PowerShell-аналоги
.\scripts\backup.ps1
.\scripts\verify-backup.ps1 -Create
.\scripts\restore.ps1 -DumpFile .\backups\database\finspace_....dump
.\scripts\backup-cleanup.ps1
```

`backup-verify` создаёт custom-format dump, проверяет SHA-256 manifest, восстанавливает
его во временную отдельную БД, сверяет Alembic revision/таблицы/read-only запрос и удаляет
только временную БД. Обычный restore не затирает рабочую БД. Инструкция и аварийный
сценарий: [docs/backup-and-restore.md](docs/backup-and-restore.md).

## Импорт CSV/XLSX

Раздел **Импорт** реализует четыре явных шага:

1. upload создаёт batch и staging rows, но не transactions;
2. mapping сопоставляет исходные столбцы с целевыми полями;
3. validate нормализует значения, проверяет справочники и находит дубли;
4. commit с подтверждением и idempotency key создаёт только valid rows одной операцией.

Импортированный batch можно откатить soft-delete. Изменённые после импорта операции
дают конфликт без явного force. Форматы, локали, поиск дублей и ограничения описаны в
[docs/import.md](docs/import.md).

## Проверки

Backend работает с отдельной `TEST_DATABASE_URL` и проверяет миграционный цикл
вплоть до `0006_automations_telegram`, проверяет цикл `0005 → 0006 → 0005 → 0006`, затем
удаляет только уникальную тестовую БД.

```powershell
make test
make auth-test
make import-test
make google-test
make sync-test
make reconciliation-test
make test-db-safety
make automation-test
make telegram-test
make recurring-test
make month-close-test
make backup-secondary-test
make google-config-check
# После настройки отдельного тестового Google account и HTTPS tunnel:
make google-live-acceptance
make google-live-report ACCEPTANCE_RUN_ID=<uuid>
make google-live-cleanup ACCEPTANCE_RUN_ID=<uuid>
make backup-verify
docker compose exec backend ruff check .
docker compose exec backend ruff format --check .
docker compose exec backend mypy app

docker compose exec frontend npm test
docker compose exec frontend npm run lint
docker compose exec frontend npm run typecheck
docker compose exec frontend npm run build
```

## Управление средой

```powershell
.\scripts\dev-logs.ps1
.\scripts\dev-down.ps1
.\scripts\dev-reset.ps1
```

`dev-reset.ps1` удаляет volumes только после ввода `RESET`. Обычный
`docker compose down` сохраняет PostgreSQL, Redis, импортированные artifacts и backups.

## Документация

- [Архитектура](docs/architecture.md)
- [Локальная разработка](docs/local-development.md)
- [Production deployment frontend](docs/frontend-production.md)
- [Модель безопасности](docs/security.md)
- [Локальный n8n](docs/n8n.md)
- [Безопасность автоматизаций](docs/automation-security.md)
- [Telegram](docs/telegram.md)
- [Регулярные операции](docs/recurring-rules.md)
- [Недельные отчёты](docs/weekly-reports.md)
- [Закрытие месяца](docs/month-close.md)
- [Backup и restore](docs/backup-and-restore.md)
- [Staging-импорт](docs/import.md)
- [Основная Google-книга](docs/google-sheets.md)
- [Apps Script Bridge](docs/apps-script-bridge.md)
- [Google Sheets без Cloud Console](docs/google-without-cloud-console.md)
- [Google OAuth](docs/google-oauth.md)
- [Apps Script](docs/google-apps-script.md)
- [Защита тестовой БД](docs/test-database-safety.md)
- [Живая приёмка Google Sheets](docs/google-live-acceptance.md)
- [ADR 0005: test isolation и живая Google-приёмка](docs/decisions/0005-test-isolation-and-live-google-acceptance.md)
- [ADR 0006: Apps Script Bridge](docs/decisions/0006-apps-script-bridge.md)
- [ADR 0007: граница n8n/Backend](docs/decisions/0007-n8n-automation-boundary.md)
- [Синхронизация](docs/synchronization.md)
- [Конфликты и сверка](docs/sync-conflicts.md)
- [ADR 0001: локальная Docker-архитектура](docs/decisions/0001-local-docker-architecture.md)
- [ADR 0002: финансовое ядро](docs/decisions/0002-financial-core.md)
- [ADR 0003: auth, backup и import](docs/decisions/0003-auth-backup-import.md)
- [ADR 0004: Google Sheets sync](docs/decisions/0004-google-sheets-sync.md)
