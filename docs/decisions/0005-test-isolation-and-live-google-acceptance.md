# ADR 0005: изоляция тестовой БД и живая приёмка Google

- Статус: принято
- Дата: 2026-07-22

## Контекст

Один запуск pytest унаследовал development `DATABASE_URL`, потому что `tests/conftest.py`
использовал `os.environ.setdefault`. Shell-scoping различается между PowerShell, Make и sh,
поэтому само соглашение о запуске не является защитой. Google-интеграция при этом проверена
mock-клиентом, но ещё не прошла живую приёмку.

## Решение: тестовая БД

Единственный полный test runner создаёт отдельную базу
`finspace_test_<test_run_id>`, передаёт её URL непосредственно процессам Alembic и pytest,
а затем удаляет базу в `finally`. Он не меняет и не очищает постоянную `finspace_test`.

Любой pytest-процесс до импорта приложения проверяет:

1. `TESTING=true`;
2. `ENVIRONMENT != production`;
3. имя БД соответствует `*_test`, `test_*` или `finspace_test_*`;
4. имя не равно `finspace`, `postgres`, `production` или `prod`;
5. в `system_metadata` есть `test_database_marker={"testing": true}`.

Migration test cycle дополнительно требует `MIGRATION_TEST_CYCLE=true`. Runner
проверяет marker перед циклом `head → 0004_google_sheets_sync → head` и после него.
Таблица `system_metadata` из revision 0001 в этом цикле сохраняется. Если удаление
временной БД не удалось, runner явно печатает её точное имя.

Прямой `pytest`, test seed и test cleanup без этих условий завершаются до изменения БД.
Постоянные development/production migration и seed не используют test-команды и не получают
`TESTING=true`.

## Решение: живая Google-приёмка

Живая приёмка получает отдельный `acceptance_run_id`. Локальный registry связывает с ним
созданные user/workspace, binding, spreadsheet и публичный tunnel URL, но никогда не содержит
пароль, token, OAuth code, client secret, HMAC secret или encryption key.

Acceptance cleanup разрешён только в `development`, требует точный run ID и registry,
сверяет UUID workspace и префикс `acceptance-<run_id>`, после чего удаляет только явно
перечисленные связанные объекты. Это отдельная команда, а не универсальный SQL cleanup.
Google grant и файл сначала отключаются/отзываются штатными endpoint/UI; tunnel и Apps Script
trigger отключаются вручную и фиксируются в отчёте.

## Последствия

- Ошибка shell-scoping больше не может направить pytest в development БД.
- Тесты немного медленнее из-за создания/удаления уникальной БД.
- Наличие marker проверяется независимо от имени БД.
- Живой OAuth остаётся интерактивным и возможен только при локально настроенных credentials.
- Acceptance report публикует только идентификаторы, counts, статусы и замаскированные hashes.
