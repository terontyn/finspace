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

`ru-RU` трактует slash/dash даты как day/month, `en-US` — как month/day; ISO `YYYY-MM-DD`
одинаков для обеих локалей. Пустые строки получают `skipped`. Сумма должна быть строго
положительной: направление задаёт тип операции или отдельная income/expense колонка.
Категория обязана соответствовать income/expense типу. Статус `reconciled` импортом не
назначается, потому что он требует отдельного account reconciliation evidence.

`occurred_at` хранится в UTC, но диапазон дат preview вычисляется в timezone workspace,
поэтому операция около полуночи не отображает предыдущий календарный день. Mapping можно
менять только до commit/cancel; терминальный batch нельзя вернуть в `parsed`.

## Дубликаты

Fingerprint включает workspace, occurred date/time, type, amount, currency, исходный и
целевой счета, нормализованное description и external ID. Совпадение получает статус
`duplicate` и по умолчанию не импортируется. Пользователь может явно пометить конкретную
строку «Это новая операция» через `PATCH /imports/{batch}/rows/{row}`; действие попадает
в audit.

## Commit и rollback

Commit блокирует неподготовленный/повторный batch, идемпотентно принимает тот же key,
создаёт provenance `source=import`, `import_batch_id` и
`created_transaction_id`. Ошибка откатывает всю DB-транзакцию.

Batch блокируется `FOR UPDATE` на mapping/validation/finalize/rollback/cancel, поэтому два
конкурентных finalize не могут создать две группы операций.

`POST /api/v1/imports/{id}/rollback` soft-delete только операции этого batch. Повторный
rollback безопасен. Если операция после импорта изменилась (`version != 1`), обычный
rollback возвращает `IMPORT_ROLLBACK_CONFLICT`; явный `force=true` нужен отдельно.
Ручные операции и другие workspaces не затрагиваются.

Ограничения: нет Google Sheets, OCR, банковских форматов/автодетекторов, aliases и
автоматического создания счетов/категорий.

## Staging-файлы: что это и что с ними происходит

В `./data/imports` лежат **только** загруженные файлы под случайными именами вида
`<32 hex>.csv` и `<32 hex>.xlsx`. Подкаталогов там нет.

> [!IMPORTANT]
> **Это не резервная копия.** Содержимое файла разбирается один раз, при загрузке, и
> переносится в `import_rows`; после этого файл не читается никогда. Финансовая истина
> после commit — PostgreSQL: `transactions`, `import_batches`, `import_rows`. Потеря
> staging-файла не теряет ни одной операции. Для восстановления служит
> [backup-and-restore.md](backup-and-restore.md), а не этот каталог.

Штатно файл удаляется сразу:

| момент | что происходит с файлом |
|---|---|
| ошибка загрузки | удаляется в том же запросе |
| `commit` (`imported`) | удаляется сразу после фиксации транзакции |
| `cancel` (`cancelled`) | удаляется сразу после смены статуса |
| `rollback` (`rolled_back`) | к этому моменту уже удалён при commit |

Поэтому каталог не растёт при нормальной работе. Остаться в нём файл может только после
аварийной остановки процесса: между записью файла и фиксацией строки в БД, либо между
фиксацией commit/cancel и удалением файла.

## Освобождение staging (reclamation)

Уборка удаляет **только** такие остатки и никогда не работает «по возрасту».

Классификация каждого файла в каталоге:

| класс | условие | действие |
|---|---|---|
| `reclaimable_terminal` | ровно один batch, статус `imported`, `rolled_back` или `cancelled` | можно удалить |
| `reclaimable_orphan` | ни один batch не ссылается на файл, имя из управляемого пространства, возраст больше grace | можно удалить |
| `active` | ровно один batch, статус `uploaded`, `mapping_required`, `parsed`, `validated`, `ready` или `importing` | **никогда** не удаляется |
| `orphan_within_grace` | нет ссылки, но файл моложе grace | не удаляется |
| `ambiguous` | на файл ссылается больше одного batch | не удаляется |
| `unknown` | каталог, symlink, нерегулярный файл, незнакомый статус, имя вне управляемого пространства (в том числе `.gitkeep` и старые артефакты) | не удаляется |

Незавершённый импорт не исчезает никогда, сколько бы он ни лежал: TTL у batch нет, и
пользователь вправе вернуться к нему через недели.

**Уборка никогда не удаляет** строки БД: ни `import_batches`, ни `import_rows`, ни
`transactions`, счета, категории, сверки, закрытые месяцы и историю категоризации. Она
работает только с файлами в `data/imports`.

### Осмотр без удаления

```bash
cd /opt/finspace
sudo finspace-compose run --rm --no-deps backend python scripts/import_staging_reclaim.py
sudo finspace-compose run --rm --no-deps backend python scripts/import_staging_reclaim.py --json
```

Запуск без аргументов **ничего не удаляет** — это режим по умолчанию. Отчёт показывает
общее число файлов и байт, сколько из них подлежит освобождению, и разбивку по классам.

### Удаление

```bash
sudo finspace-compose run --rm --no-deps backend python scripts/import_staging_reclaim.py --apply
```

Удаляются только файлы классов `reclaimable_*`, не более
`IMPORT_STAGING_RECLAIM_BATCH_SIZE` за запуск. Повторный запуск безопасен и сходится:
уже удалённый файл считается `already_absent`, а не ошибкой.

### Настройки

| переменная | по умолчанию | смысл |
|---|---|---|
| `IMPORT_STAGING_RECLAIM_ENABLED` | `true` | `false` запрещает `--apply`; осмотр продолжает работать |
| `IMPORT_STAGING_RECLAIM_GRACE_HOURS` | `72` | **часы**; применяется только к файлам без ссылки из БД |
| `IMPORT_STAGING_RECLAIM_BATCH_SIZE` | `200` | максимум удалений за один запуск |

Grace нужен из-за загрузки «в полёте»: файл уже создан, а строка ещё не зафиксирована.
Значение должно с запасом превышать самую долгую загрузку — отсюда 72 часа, а не минуты.
Недопустимые значения отвергаются валидацией настроек при старте.

### Автоматизация (необязательно)

```bash
cd /opt/finspace
sudo install -m 0644 infrastructure/systemd/finspace-import-reclaim.service /etc/systemd/system/
sudo install -m 0644 infrastructure/systemd/finspace-import-reclaim.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finspace-import-reclaim.timer
systemctl list-timers finspace-import-reclaim.timer
```

Раз в неделю, воскресенье 03:30 по локальному времени хоста, с наверстыванием пропуска —
намеренно далеко от окна backup. Отключить автоматику: `sudo systemctl disable --now
finspace-import-reclaim.timer`, либо централизованно `IMPORT_STAGING_RECLAIM_ENABLED=false`
в `.env` (тогда `--apply` откажется работать и на всех хостах сразу).

Установка таймера необязательна: без него уборка выполняется командой оператора.

### Поведение при отказе

- БД недоступна или ответила ошибкой — не удаляется **ничего**: запрос к БД выполняется до
  первого удаления;
- отдельный файл не удалился (права, гонка) — он попадает в `failures`, остальные
  кандидаты обрабатываются как обычно, объём удаления не расширяется;
- команда завершается ненулевым кодом, если были `failures`.

После сбоя достаточно повторить команду: операция идемпотентна. Ничего восстанавливать не
нужно — удаляются только файлы, которые уже не нужны ни одному сценарию.

### Первый запуск после обновления

Старые артефакты, имена которых не соответствуют текущему пространству имён и на которые
не ссылается ни один batch, попадают в `unknown`: они показываются в отчёте и **не
удаляются**. Разбираться с ними — отдельное осознанное решение оператора, а не побочный
эффект обновления.
