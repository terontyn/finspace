# Backup и restore PostgreSQL

## Создание

```powershell
make backup
# или
.\scripts\backup.ps1
```

Вызываемый service `backup` использует официальный PostgreSQL 17 и переменные `PG*`;
пароль не попадает в имя файла или аргументы процесса. Скрипт проверяет `pg_isready`,
делает `pg_dump --format=custom`, ненулевой размер, `pg_restore --list`, SHA-256 и пишет
manifest атомарно. `.partial` удаляется при ошибке.

Пример manifest:

```json
{
  "filename": "finspace_2026-07-22T120000Z.dump",
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "created_at": "2026-07-22T12:00:00Z",
  "database": "finspace",
  "alembic_revision": "0006_automations_telegram",
  "format": "postgresql-custom",
  "size_bytes": 123456
}
```

## Автоматическая проверка

```powershell
make backup-verify   # создать новую копию и проверить restore
make restore-test    # проверить последнюю существующую копию
.\scripts\verify-backup.ps1 -Create
```

Команда сверяет manifest/SHA, создаёт уникальную временную БД, выполняет restore,
сравнивает Alembic revision, проверяет основные таблицы и read-only запрос к
`system_metadata`, затем удаляет временную БД даже при ошибке. Любая ошибка даёт
ненулевой exit code. В audit основной БД появляются `backup.created`,
`backup.verified`, `restore.verified`.

Проверка также требует таблицы Google sync и новые таблицы service accounts, automation
runs, recurring rules, Telegram intents, month closures и notification settings. Google
tokens входят в dump только как ciphertext. Encryption
key берётся из `.env`/secret и **не входит** в backup: без сохранённого внешнего ключа и его
версии восстановленные Google tokens расшифровать нельзя, потребуется reconnect.

## Ручное безопасное восстановление

По умолчанию рабочая dev-БД не затрагивается:

```powershell
.\scripts\restore.ps1 `
  -DumpFile .\backups\database\finspace_2026-07-22T120000Z.dump `
  -TargetDatabase finspace_restore_test
```

Указывается точный существующий dump. Shell-вариант внутри tools-контейнера:

```bash
docker compose --profile tools run --rm backup \
  sh /scripts/restore.sh /backups/database/finspace_2026-07-22T120000Z.dump finspace_restore_test
```

Перезапись основной БД требует одновременно точного пути, `--overwrite-main` и строки
`OVERWRITE finspace` (либо точного значения `POSTGRES_DB`). Перед этим остановите backend
и frontend, создайте дополнительную копию и убедитесь, что `backup-verify` зелёный.

## Acceptance после Google integration

Живой acceptance требует `make backup-verify` после создания binding. Временная
restore-БД должна иметь revision `0006_automations_telegram` и содержать bindings, outbox,
inbox, conflicts и sync runs. Проверка отдельно подтверждает binding columns `provider`,
`binding_secret_hash`, secret timestamps, heartbeat, pull и ACK. Bridge secret plaintext
никогда не входит в dump. Token ciphertext и внешний encryption key относятся только к
необязательному OAuth provider.
Filename, сокращённый SHA-256, revision и restore result фиксируются в локальном
acceptance report до точечной очистки.

## Retention

```powershell
make backup-cleanup
.\scripts\backup-cleanup.ps1
```

Сохраняются последние `BACKUP_RETENTION_DAILY=7` и по одной копии последних
`BACKUP_RETENTION_WEEKLY=4` недель. Скрипт работает только с `finspace_*.dump` внутри
настроенного каталога и никогда не удаляет единственную копию.

## Вторичная локальная копия

После успешной verify можно включить абстрактный provider `local_secondary_path`:

```env
BACKUP_REMOTE_PROVIDER=local_secondary_path
BACKUP_SECONDARY_PATH=D:/finspace-secondary
BACKUP_REMOTE_AFTER_VERIFY=true
```

Копируются только проверенные dump и manifest через временные `.partial`; перед
публикацией повторно сверяются SHA-256 и `pg_restore --list`. Событие
`backup.remote.copy` содержит только безопасные метаданные. Это не облако и не защита от
компрометации компьютера; provider `disabled` остаётся значением по умолчанию.

n8n volume в PostgreSQL backup не входит. Его следует копировать отдельно в остановленном
состоянии вместе с внешне сохранённым `N8N_ENCRYPTION_KEY`; bot token, ServiceKey и сам
encryption key не должны попадать в dump/manifest или Git. Шифрование backup и managed
remote storage пока отсутствуют.
