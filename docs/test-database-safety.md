# Безопасность тестовой базы данных

Все автоматические backend-тесты запускаются только через единый runner. Он создаёт
уникальную БД `finspace_test_<32 hex>`, передаёт её URL непосредственно процессам
Alembic и pytest, а затем удаляет ровно эту БД в `finally`.

## Обязательные барьеры

Любая test-команда обязана пройти проверки до первой SQL-команды:

1. `TESTING=true`;
2. `ENVIRONMENT` не равен `production`;
3. имя БД оканчивается на `_test`, начинается с `test_` или точно соответствует
   `finspace_test_<test_run_id>`;
4. имена `finspace`, `postgres`, `production` и `prod` запрещены;
5. перед pytest, migration-cycle и cleanup существует
   `system_metadata.key = test_database_marker` со значением `testing=true`;
6. для runner-БД UUID в имени, `TEST_RUN_ID` и marker совпадают.

Первичный `alembic upgrade head` допускается только после статической проверки
уникального имени. После появления `system_metadata` отдельный test-seed записывает
marker. Все последующие destructive-операции требуют marker.

`tests/conftest.py` не меняет окружение и не подставляет test URL. Поэтому прямой
`pytest` с development URL завершается ошибкой ещё до импорта приложения.

## Запуск

```powershell
make test-db-safety
make test

# PowerShell
.\scripts\test-db-safety.ps1
```

Runner использует `TEST_DATABASE_URL` только как безопасную основу для подключения
к PostgreSQL. Значение `DATABASE_URL` дочернему процессу передаётся через его
собственное окружение; shell-scoping не используется. Redis тестов изолирован через
`TEST_REDIS_URL`, по умолчанию database 15.

## Доказательство отказа

`test_db_safety_check.py` подставляет URL БД `finspace` и проверяет ненулевой exit code
с префиксом `TEST DATABASE SAFETY` для:

- pytest collection;
- migration test cycle;
- test seed;
- test cleanup.

Проверка не создаёт, не мигрирует и не очищает development-БД. Cleanup принимает
только точные `database_url` и `test_run_id`, сверяет marker и не содержит wildcard,
`LIKE` или удаления по общему признаку.

Если автоматическое удаление не удалось, runner выводит точное имя оставшейся БД.
Её можно сначала проверить без удаления, затем удалить явно:

```powershell
docker compose exec -e TESTING=true backend python scripts/test_database_cleanup.py `
  --database-url "postgresql+asyncpg://.../finspace_test_<run-id-without-dashes>" `
  --test-run-id "<run-id>"

docker compose exec -e TESTING=true backend python scripts/test_database_cleanup.py `
  --database-url "postgresql+asyncpg://.../finspace_test_<run-id-without-dashes>" `
  --test-run-id "<run-id>" --execute
```

