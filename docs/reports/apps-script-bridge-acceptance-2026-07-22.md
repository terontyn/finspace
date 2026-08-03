# Apps Script Bridge — живая приёмка 2026-07-22

## Итог

Этап 5 завершён успешно. Реальная Google-книга обменивалась искусственными финансовыми данными с локальным backend через временный HTTPS tunnel в обоих направлениях. Все 25 обязательных evidence-пунктов получили статус `passed`; после формирования снимка выполнена точечная очистка текущего acceptance run.

- Acceptance run: `086af5f7-0d45-4dfa-95c2-f97da4fb159c`
- Префикс искусственных данных: `ASB-ACCEPT-086AF5F7`
- Реальные финансовые сведения не использовались.
- Google Cloud OAuth credentials не применялись и для provider `apps_script_bridge` не требовались.
- Учётные данные, HMAC-материал, JWT, cookie, database URL и содержимое `.env` в отчёт не включены.

## Окружение

| Параметр | Результат |
|---|---|
| Режим | local development, Docker Compose |
| Migration revision | `0005_apps_script_bridge` |
| Provider | `apps_script_bridge` |
| Template version | `1` |
| Apps Script version | `1` |
| Финальный health | `ok` |
| Финальный readiness | `ready` |

## Binding и регистрация

- Binding ID в маскированном виде: `de914183…86ff`.
- До cleanup binding был `active`; heartbeat после финальной ротации успешен.
- В PostgreSQL подтверждён только 64-символьный SHA-256 hash binding credential.
- Значение credential хранилось в Apps Script Document Properties и не записывалось в ячейки книги.
- Повторная регистрация исходной книги вернула `already_registered`.
- Попытка зарегистрировать другую книгу с тем же binding отклонена: HTTP 409 `APPS_SCRIPT_REBIND_REQUIRED`.
- Старое значение после ротации отклонено как `APPS_SCRIPT_SIGNATURE_INVALID`; новое значение прошло heartbeat.
- При cleanup binding последовательно переведён в `paused`, архивирован и удалён вместе с тестовым workspace.

## Template и initial export

- Созданы 13 требуемых листов, включая скрытые `_sync_meta` и `_lists`.
- Проверены скрытые технические колонки, warning-protection, frozen headers, filters, validation, named ranges и conditional formatting.
- Initial export сформировал 10 канонических строк: 3 счёта, 3 категории и 4 операции.
- UUID, version и row hash заполнены; ACK завершил все 10 initial export events.
- Повторный pull вернул 0 событий и не создал дубли.

## Application → Google Sheets

- Операция `450.75 RUB` создана в PostgreSQL, попала в outbox, появилась в строке 6 и была завершена ACK.
- Pause/resume: запись `641.25 RUB` сохранилась в PostgreSQL и pending outbox при pause, не выдавалась pull и была доставлена после resume.
- Lease: первый pull получил событие без ACK, конкурентный pull получил 0; после контролируемого истечения lease событие выдано повторно и завершено ACK со второй попытки.
- Перед cleanup в snapshot было 34 outbox-записи: 31 `completed` и 3 намеренно оставленных `pending` артефакта сценариев задержки/отрицательных проверок. Все они принадлежали acceptance workspace и удалены точечно.

## Google Sheets → Application

- Новая строка создала ровно одну операцию `350.50 RUB`; localized input `350,50` нормализован корректно.
- Повторная отправка того же event ID не создала вторую запись.
- Редактирование `350.50 → 375.75` сохранило UUID, увеличило version `1 → 2`, изменило row hash и записало audit before/after.
- Отдельная HMAC-проверка: первое событие применено, тот же nonce отклонён HTTP 409 `APPS_SCRIPT_REPLAY_DETECTED`, тот же event ID с новым nonce классифицирован как `duplicate`. В БД существовали ровно одна transaction и одна inbox-запись.
- Payload с чужим spreadsheet ID отклонён как `WORKSPACE_ACCESS_DENIED` без изменения финансовых данных.

## Конфликты

Проверены три независимых конфликта версий:

| Resolution | Итог |
|---|---|
| `keep_database` | Значение БД `721.10`, version 3, строка возвращена в `SYNCED` |
| `keep_sheet` | Значение Sheets `732.20` применено как version 4, строка `SYNCED` |
| `manual_merge` | Результат `743.30`, version 4, строка `SYNCED` |

Для каждого сценария подтверждены запись PostgreSQL, version, row hash, audit и состояние конфликта.

## Reconciliation

Расширенная живая сверка завершилась с точной матрицей:

```json
{"matched":13,"conflict":1,"sheet_newer":1,"duplicate_in_sheet":2,"technical_tamper":1,"database_newer":1,"unknown_in_sheet":1,"missing_in_sheet":1}
```

- Missing row восстановлена из канонических данных.
- Database-newer row восстановлена до `650.25`, version 2.
- Изменённая пользователем строка сохранена как `CONFLICT`, без молчаливого затирания.
- Technical tamper восстановлен до канонической version/hash; PostgreSQL не изменился.
- Дубликаты и неизвестный UUID не создали новые финансовые сущности.
- Две ожидаемые reconciliation-конфликтные строки остались открыты до точечного cleanup.

## Недоступность backend

- После остановки только backend Apps Script получил HTTP 502, а строка 13 осталась `PENDING` в локальной очереди.
- Триггеры были удалены до восстановления, исключив автоматическую гонку.
- После возврата публичного health единственная queued-запись отправилась: `Обработано: 1, осталось: 0`.
- Строка стала `SYNCED`, сумма — `651.25`, version — 3.
- PostgreSQL подтвердил ровно одну `applied` inbox-запись для этой операции.

## Apps Script triggers

- Установлены installable `onEdit`, time-driven sync и `onOpen`.
- Повторная установка была идемпотентной.
- Два независимых удаления каждый раз удаляли ровно 3 обработчика Финпространства, что исключает накопление дублей и затрагивание посторонних triggers.
- Финальный reset очистил Document Properties и локальную очередь.

## Backup и restore

- Файл: `finspace_2026-07-22T184505Z.dump`.
- Сокращённый SHA-256: `abcdcd713515…`.
- Manifest и контрольная сумма совпали; custom dump прошёл `pg_restore --list`.
- Restore выполнен в изолированную временную БД с revision `0005_apps_script_bridge`.
- Из восстановленной БД прочитаны данные acceptance workspace: accounts 3, categories 3, transactions 13, bindings 1, outbox 34, inbox 10, conflicts 5, sync runs 3, audit 97.
- В восстановленном binding подтверждены provider `apps_script_bridge` и валидный 64-символьный hash-only credential.
- Временная restore-БД удалена после проверки.

## Обнаруженные и исправленные дефекты

- Исправлена нормализация локализованных денежных значений и повторная обработка ранее rejected event ID.
- Outbox теперь накапливает изменения при paused binding, не отдавая их до resume.
- Reconciliation защищает пользовательские строки со статусами `DIRTY`, `PENDING` и `ERROR`, классифицируя их как конфликт.
- В Apps Script исправлены HMAC byte handling, порядок pull, flush и rollback записи строки.
- Добавлены targeted regression tests; соответствующие pytest-проверки и Ruff завершились успешно.

## Cleanup

Cleanup выполнен только для acceptance run `086af5f7-0d45-4dfa-95c2-f97da4fb159c` по заранее сохранённым UUID.

| Объект | Удалено |
|---|---:|
| users / workspaces / workspace_members | 1 / 1 / 1 |
| auth_sessions | 1 |
| accounts / categories / transactions | 3 / 3 / 13 |
| google_sheet_bindings | 1 |
| sync_outbox / sync_inbox | 34 / 10 |
| sync_conflicts / sync_runs | 5 / 3 |
| audit_log | 99 |
| OAuth connections / flows | 0 / 0 |
| imports / splits | 0 |

После cleanup:

- acceptance workspace, user и binding: 0;
- все строки с acceptance workspace ID в проверенных таблицах: 0;
- контрольные количества данных вне acceptance workspace полностью совпали до и после удаления;
- тестовая Google-книга перемещена пользователем в корзину;
- triggers удалены, Document Properties очищены;
- временный tunnel остановлен;
- `PUBLIC_BACKEND_URL` очищен в локальном `.env`;
- backend и sync-worker пересозданы; health `ok`, readiness `ready`.

## Артефакты

- Безопасный JSON snapshot и cleanup evidence: `backups/acceptance-reports/google-live-acceptance-086af5f7-0d45-4dfa-95c2-f97da4fb159c.json`.
- Реестр run: `data/acceptance/086af5f7-0d45-4dfa-95c2-f97da4fb159c.json`.
- Этот отчёт: `docs/reports/apps-script-bridge-acceptance-2026-07-22.md`.

Git commit не создавался.
