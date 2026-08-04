# Локальная разработка

## Жизненный цикл

1. Скопируйте `.env.example` в `.env` и замените пароль PostgreSQL/JWT secret.
2. Запустите `docker compose up -d --build`.
3. Выполните `docker compose exec backend alembic upgrade head`.
4. Зарегистрируйтесь через `/register` или задайте пароль dev-пользователю через `/login`.
5. Перед передачей изменений запустите все проверки.
6. Остановите среду через `docker compose down` без `--volumes`.

## Auth в development

`POST /api/v1/dev/bootstrap` доступен только при `ENVIRONMENT=development`. Он создаёт
тестовые справочники, но не выдаёт авторизацию. UI предлагает задать bootstrap-пользователю
пароль через `POST /api/v1/auth/set-development-password`.

`ALLOW_DEV_AUTH_HEADERS=false` по умолчанию. Временную совместимость можно включить
только в development; production-конфигурация с этим флагом не запускается. Обычные
запросы используют `Authorization: Bearer ...`; refresh cookie браузер отправляет сам.

## Backend

```powershell
docker compose exec backend ruff format .
docker compose exec backend ruff check .
docker compose exec backend mypy app
docker compose exec backend sh scripts/run-tests.sh
make auth-test
make import-test
make google-test
make sync-test
make reconciliation-test
```

Не редактируйте применённые миграции и не используйте `create_all()`. Новая миграция
должна корректно проходить upgrade/downgrade. Основная dev-БД тестами не очищается.

## Google integration

Backend запускается и без `GOOGLE_CLIENT_ID`/secret. Для ручной проверки следуйте
`docs/google-oauth.md`, затем запустите `make sync-worker`. Apps Script не видит localhost;
temporary HTTPS tunnel настраивается вручную по `docs/google-apps-script.md`. Не запускайте
tunnel автоматически и выключайте его после smoke test.

```powershell
.\scripts\google-status.ps1 -AccessToken $token -WorkspaceId $workspace
.\scripts\google-full-export.ps1 -AccessToken $token -WorkspaceId $workspace
.\scripts\google-reconcile.ps1 -AccessToken $token -WorkspaceId $workspace
.\scripts\sync-logs.ps1
```

## Frontend

Вся HTTP-логика проходит через `frontend/src/lib/api-client.ts`. Access token нельзя
переносить в localStorage/sessionStorage. `NEXT_PUBLIC_API_URL=/` оставляет `/api/*`
на origin frontend; Next.js проксирует эти запросы к `INTERNAL_API_URL` во внутренней
Docker-сети. Обе переменные не являются секретами. Старый `/local-api` rewrite оставлен
только для совместимости существующих локальных `.env`.

```powershell
docker compose exec frontend npm test
docker compose exec frontend npm run lint
docker compose exec frontend npm run typecheck
docker compose exec frontend npm run build
```

## n8n и автоматизации

Задайте случайный `N8N_ENCRYPTION_KEY` в `.env`, затем:

```powershell
make n8n-up
make n8n-status
make n8n-import
make automation-test
make telegram-test
make recurring-test
make month-close-test
```

n8n доступен только на <http://localhost:5678>. Не добавляйте его в Cloudflare tunnel и
не подключайте к PostgreSQL/Redis. ServiceKey и Telegram bot token создаются/хранятся в
n8n credentials, а не в workflow JSON. Подробнее: [n8n.md](n8n.md).

## Backup и import artifacts

`./backups/database` и `./data/imports` bind-mounted, исключены из Git и имеют только
placeholder-файлы. Не прикладывайте dump, manifest с рабочими именами БД или исходные
финансовые файлы к issue/commit.

```powershell
make backup-verify
make backup-cleanup
```

## Диагностика

- `docker compose ps` — healthchecks;
- `docker compose logs --follow backend` — HTTP и request IDs;
- `docker compose exec backend alembic current` — revision;
- `Invoke-RestMethod http://localhost:8000/api/v1/health/ready` — зависимости;
- `docker compose exec postgres psql -U finspace -d finspace` — SQL-консоль;
- `docker compose --profile tools run --rm backup pg_restore --version` — backup tools.
- `.\scripts\n8n-status.ps1` — health и Compose status n8n;
- `docker compose logs --tail 100 n8n` — ошибка ключа, owner setup или workflow.

Не публикуйте `.env`, полный `docker compose config`, cookie или токены.
