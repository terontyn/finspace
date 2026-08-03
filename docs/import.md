# Безопасный импорт CSV/XLSX

## Поддерживаемые файлы

Поддерживаются `.csv` (UTF-8/BOM или Windows-1251) и `.xlsx`. Максимальный размер и
число строк задают `IMPORT_MAX_FILE_SIZE_MB` и `IMPORT_MAX_ROWS`. CSV читается потоково,
XLSX — `read_only`, `data_only`, без сохранения external links. XLSM, неправильная
сигнатура, VBA/ActiveX и запрещённые расширения отклоняются; формулы не вычисляются.

Файл сохраняется под случайным именем в `./data/imports`; исходное имя остаётся в БД.
Пользователь не управляет путём. После commit/cancel файл удаляется, staging и hash
сохраняют историю.

## Четыре шага

1. `POST /api/v1/imports`: создаёт `import_batches`/`import_rows`, не transactions.
2. `PUT /api/v1/imports/{id}/mapping`: сохраняет явное сопоставление и locale.
3. `POST /api/v1/imports/{id}/validate`: нормализует и строит preview.
4. `POST /api/v1/imports/{id}/commit`: требует `confirm=true` и
   `X-Idempotency-Key`, атомарно создаёт только valid rows.

История, фильтры `status`, `has_errors`, `duplicate`, cancel и rollback доступны через
`/api/v1/imports`. Все endpoints требуют editor/owner и ограничены workspace.

## Mapping и нормализация

Поддерживаются единый журнал (`amount`) и отдельные `income_amount`/`expense_amount`.
Обязательны `date`, `account` и один вариант суммы. Дополнительные поля: time,
transaction_type, currency, target_account, category, counterparty, description,
comment, status, external_id.

Backend нормализует BOM/пробелы, десятичную запятую/разделители тысяч, валюту, тип,
статус, пустые значения и распространённые даты. Неоднозначные day/month даты трактует
явный locale. Счета и категории ищутся по точному нормализованному имени; неизвестное или
неоднозначное имя — ошибка строки. Alias-таблицы пока не добавлены, новые справочники
автоматически не создаются.

## Дубликаты

Fingerprint включает workspace, occurred date/time, type, amount, currency, source и
target accounts, нормализованное description и external ID. Совпадение получает статус
`duplicate` и по умолчанию не импортируется. Пользователь может явно пометить конкретную
строку «Это новая операция» через `PATCH /imports/{batch}/rows/{row}`; действие попадает
в audit.

## Commit и rollback

Commit блокирует неподготовленный/повторный batch, идемпотентно принимает тот же key,
создаёт provenance `source=import`, `import_batch_id` и
`created_transaction_id`. Ошибка откатывает всю DB-транзакцию.

`POST /api/v1/imports/{id}/rollback` soft-delete только операции этого batch. Повторный
rollback безопасен. Если операция после импорта изменилась (`version != 1`), обычный
rollback возвращает `IMPORT_ROLLBACK_CONFLICT`; явный `force=true` нужен отдельно.
Ручные операции и другие workspaces не затрагиваются.

Ограничения: нет Google Sheets, OCR, банковских форматов/автодетекторов, aliases и
автоматического создания счетов/категорий.
