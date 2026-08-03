# Живая приёмка Apps Script Bridge

Runbook предназначен только для отдельного тестового Google account, искусственного
workspace `Google Sync Acceptance Test` и временного HTTPS tunnel. OAuth Client ID, Google
Cloud billing и Google Cloud project не нужны.

## 1. Подготовка

Укажите основной provider, действительный tunnel URL и запустите:

```powershell
make google-config-check
make test
make frontend-test
```

Config check должен быть зелёным при пустых `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` и
`GOOGLE_TOKEN_ENCRYPTION_KEY`. Redis обязателен для replay protection.

Создайте изолированный acceptance run:

```powershell
make google-live-start
```

Пароль вводится интерактивно. Registry в `data/acceptance/` хранит только run/workspace/user
IDs и статусы evidence; secret, JWT, cookies, HMAC headers и row payloads туда не пишутся.

## 2. Binding и template

1. Войдите acceptance-пользователем и создайте Apps Script binding.
2. Убедитесь, что connection отсутствует, provider равен `apps_script_bridge`, а secret
   показан только в create response.
3. Получите package, вручную создайте пустую Google-таблицу и вставьте source files.
4. Выполните `setupFinspace()`: проверьте 13 листов, hidden `_sync_meta`/`_lists`, headers,
   filters, protected technical columns, named ranges, validations и conditional formatting.
5. Выполните `configureConnection()`. Повторная регистрация той же книги должна быть
   идемпотентна; другая книга должна требовать явный rebind.
6. Установите triggers и проверьте свежий heartbeat в приложении.

## 3. Initial export и оба направления

Нажмите **Получить обновления**. Проверьте, что pull выдаёт accounts/categories/transactions,
но outbox завершается только после ACK. После последнего ACK initial sync run и binding
становятся completed/active. Повторный ACK не меняет counts, а просроченный lease снова
делает неподтверждённый event доступным.

Создайте операцию в приложении: она должна появиться в outbox, затем через pull в книге с
совпадающими UUID/version/hash. Остановленный tunnel не должен терять событие.

Добавьте и измените операцию в Sheets. onEdit должен только поставить `DIRTY` и локальный
event; scheduled/manual push создаёт inbox/audit/PostgreSQL entity, возвращает normalized
row и `SYNCED`. Повторите event ID и nonce: event идемпотентен, replay nonce отклонён.

Проверьте pause/resume: paused pull возвращает пустой batch. Изменение технического column
даёт `TAMPER`/ошибку и не перезаписывает PostgreSQL.

## 4. Conflicts и reconciliation

Создайте stale-version conflict и проверьте UI diff и три решения:
`keep_database`, `keep_sheet`, `manual_merge`. Затем выполните сверку для matched row,
missing row, duplicate UUID, unknown UUID, technical tamper, sheet newer и database newer.
Missing row восстанавливается новым outbox event; удаление строки не удаляет PostgreSQL.

## 5. Evidence и backup

Обязательные evidence keys совпадают с allowlist скрипта: `binding`,
`apps_script_package`, `sheet_template`, `register`, `heartbeat`, `initial_export`,
`pull_ack`, `backend_change_pull`, `sheet_edit_push`, `hmac_replay`, `pause_resume`, три
`conflict_*`, `reconciliation`, `technical_columns`, `backup_restore`. Например:

```powershell
docker compose exec backend python scripts/google_live_acceptance.py mark `
  --run-id <run-id> --item pull_ack --status passed `
  --note "initial events acknowledged; duplicate ACK stable"
```

OAuth evidence (`oauth`, `oauth_refresh`, `oauth_disconnect_revoke`) необязательно и
фиксируется только если optional provider намеренно включён.

Выполните `make backup-verify`. Restore должен иметь revision
`0005_apps_script_bridge`, таблицы sync и новые binding columns provider/secret metadata/
heartbeat/pull/ACK. Secret plaintext не входит в dump.

Создайте отчёт:

```powershell
make google-live-report ACCEPTANCE_RUN_ID=<run-id>
```

## 6. Точная очистка

Удалите triggers, остановите tunnel и переместите тестовую Google-таблицу в корзину. Затем
отметьте gates `apps_script_trigger_disabled`, `tunnel_stopped`, `google_file_removed` и
выполните:

```powershell
make google-live-cleanup ACCEPTANCE_RUN_ID=<run-id>
```

Cleanup требует exact UUID, сохранённый report, точное совпадение workspace/user/email и
единственное membership. Удаляется явный allowlist таблиц по exact IDs; wildcard/LIKE и
поиск по слову `test` не используются.
