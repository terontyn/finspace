# Аудит Import, Google Sheets и Sync Conflicts

Проверено по production-контрактам backend и frontend в ветке
`codex/finspace-ui-integration`. PostgreSQL остаётся единственным источником финансовой
истины; UI не пишет операции по одной и не обращается к Apps Script с секретом.

## Матрица возможностей

| Capability | Existing backend support | Existing API | Existing frontend support | Missing API | Safe now |
| --- | --- | --- | --- | --- | --- |
| CSV/XLSX staging | `ImportBatch` + `ImportRow`, лимиты и безопасный parser | `POST /api/v1/imports` | Загрузка, файл, размер, формат | delimiter отдельно от `detected_format` | Да |
| Manual mapping | Явный whitelist полей, locale | `PUT /imports/{id}/mapping` | Обязательные поля и выбор колонок | Автосопоставление | Да |
| Validation/review | Decimal, timezone, account/category, currency, duplicate fingerprint | `POST /imports/{id}/validate`, `GET /rows` | valid/invalid/duplicate/skipped и причины | Warning severity как отдельная модель | Да |
| Batch commit | Одна DB-транзакция, audit, outbox, provenance | `POST /imports/{id}/commit` | Одно подтверждение batch | Нет | Да |
| Rollback | Soft delete только операций batch, version guard | `POST /imports/{id}/rollback` | Безопасный rollback без force | UI для осознанного force | Частично |
| Google product status | Binding, heartbeat, sync timestamps, inbox/outbox/conflicts/error | `GET /google-sheets/status` | Product overview + diagnostics | История ошибок | Да |
| Browser «sync now» для Bridge | Apps Script делает pull/push по trigger/меню | Нет browser-команды | Ссылка на книгу и точная инструкция | Серверный request/wakeup protocol | Нет, не имитировать |
| Apps Script setup/rebind | Одноразовый secret, package, pause/resume/rebind | `/apps-script/binding*`, `/package` | Настройки под раскрывающимся блоком | Нет | Да |
| Conflict list/diff | Snapshot полей и versions | `GET /conflicts?status=` | Полевой diff, filter, technical snapshots | Человекочитаемые имена связанных UUID | Да |
| Atomic conflict resolution | keep database/sheet/manual через backend service | `POST /conflicts/{id}/resolve` | Отдельные подтверждаемые действия | Bulk resolve | Да |
| Reconciliation | Apps Script snapshot и OAuth reconciliation | HMAC `/apps-script/reconcile`, OAuth `/reconcile` | Технический статус | Browser-команда для Bridge | Только существующий contour |

## Реальный lifecycle импорта

```text
upload transaction: uploaded -> mapping_required
mapping_required -> parsed
parsed/validated/ready -> validate -> ready (есть valid) | validated (valid = 0)
ready -> importing -> imported
imported -> rolled_back
mapping_required/parsed/validated/ready -> cancelled

row: raw -> valid | invalid | duplicate | skipped
valid -> imported -> rolled_back
duplicate -> valid (только explicit import_as_new)
```

`uploaded`, `importing` — промежуточные состояния внутри серверной транзакции. Finalize
принимает `confirm=true` и idempotency key, блокирует batch row и создаёт все valid rows,
audit и outbox атомарно. При исключении транзакция откатывается целиком.

Пустая строка получает `skipped`. Неверная сумма, отрицательная/нулевая сумма, неизвестный
или неоднозначный счёт/категория, несовместимый тип категории, другая валюта относительно
счёта и неверная дата получают `invalid`. `reconciled` нельзя назначить импортом: этот
статус принадлежит account reconciliation flow.

## Duplicate semantics

- Повторный файл определяется SHA-256 внутри workspace. `force_duplicate=true` создаёт
  отдельный staging batch, но не обходит проверку строк.
- Строка сравнивается по workspace, времени, типу, Decimal amount, currency, source/target
  account, нормализованному description и external ID.
- Дубликат внутри файла и совпадение с PostgreSQL получают `duplicate` и не входят в
  commit, пока пользователь явно не выберет «импортировать как новую».
- Повторный finalize с тем же idempotency key возвращает исходный результат; другой key
  для импортированного batch получает `409`.

## Google Sheets и границы доверия

```text
Finspace service -> PostgreSQL + audit + sync_outbox
Apps Script HMAC pull -> lease -> write workbook -> ACK

Google Sheets edit -> Document Properties queue -> HMAC push
-> sync_inbox -> validation + optimistic locking
-> PostgreSQL + audit + outbox | sync_conflict
```

Bridge проверяет binding, workbook, HMAC timestamp/body hash/signature и одноразовый Redis
nonce. Secret хранится только hash-ом на backend и один раз передаётся владельцу для
Document Properties; он не входит в status API, client bundle, snapshots или логи.
Binding, inbox/outbox и conflicts ограничены workspace. Произвольная другая книга
отклоняется. Одна книга на workspace — текущая намеренная граница.

## Conflict semantics

Конфликт хранит безопасные snapshots, `database_version`, `sheet_version` и список полей.
Resolution принадлежит backend и выполняется одной транзакцией. Перед решением conflict и
entity блокируются; если текущая entity version уже не совпадает с snapshot, backend
возвращает `GOOGLE_SYNC_CONFLICT_STALE` (409). Пользователь должен обновить список и решить
актуальное расхождение. Повторное решение уже закрытого конфликта также возвращает 409.

`keep_database` ставит текущую canonical строку в outbox. `keep_sheet` и `manual_merge`
повторно проходят server-side validation через inbox/application service. Отдельного
frontend `PATCH entity -> DELETE conflict` нет. Bulk resolution намеренно отсутствует.

## Requires API support

- direct «sync now»/wakeup Apps Script из browser;
- отдельные detected delimiter и warning severity для import;
- безопасный forced rollback UX с подробным impact preview;
- история интеграционных ошибок и человекочитаемое разрешение связанных UUID в diff;
- атомарный bulk conflict resolution;
- arbitrary multi-workbook.
