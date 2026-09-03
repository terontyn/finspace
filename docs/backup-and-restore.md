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

## Backup set: что именно составляет полный backup одного запуска

PostgreSQL dump остаётся канонически в `/backups/database` — backend читает его manifest из
`BACKUP_METADATA_PATH`, и большой dump не дублируется локально. Артефакты одного запуска
объединяет **backup set**: manifest, который ссылается на dump относительным путём.

```
backups/
  database/
    finspace_<timestamp>.dump
    finspace_<timestamp>.dump.manifest.json
  sets/
    <set_id>/
      backup-set.json            # неизменяемая опись артефактов
      backup-set-report.json     # изменяемое свидетельство проверок
      n8n-data.tar.gz            # только если запрошено явно
      n8n-data.sha256
```

`set_id` выводится из имени dump (`finspace_<set_id>.dump`), поэтому один dump — ровно один set.
Опись и свидетельство разделены намеренно: `backup-set.json` не переписывается после удалённой
проверки, `backup-set-report.json` — переписывается.

```bash
cd /opt/finspace
FINSPACE_COMMIT=$(git rev-parse HEAD) \
FINSPACE_TAG=$(git describe --exact-match --tags 2>/dev/null || true) \
sudo -E finspace-compose --profile tools run --rm backup sh /scripts/backup-set.sh \
  /backups/database/finspace_<timestamp>.dump
```

`local_verified=true` означает, что отработал существующий контракт проверки
(`verify-backup.sh`): восстановление во временную БД, сверка Alembic revision, обязательных
таблиц и колонок. Совпадения SHA-256 недостаточно.

**Гарантии согласованности.** PostgreSQL dump — один транзакционный снимок и единственный
источник финансовой истины. Необязательный архив n8n снимается позже и «холодно». Общего
атомарного момента между сервисами нет, и он не заявляется.

## Внешняя копия на другой хост

Каталог `/secondary` на том же диске — не второй домен отказа. Гарантия v1.0 — копия по SSH на
отдельный хост или NAS, выполняемая **с хоста**, а не из контейнера: ключ SSH и адрес приёмника
никогда не попадают в окружение backend, frontend или worker.

```bash
export FINSPACE_BACKUP_ROOT=/opt/finspace/backups
export FINSPACE_BACKUP_REMOTE_HOST=nas.example.internal
export FINSPACE_BACKUP_REMOTE_USER=finspace-backup
export FINSPACE_BACKUP_REMOTE_ROOT=/srv/backup
export FINSPACE_BACKUP_SSH_KEY=/etc/finspace/id_backup       # chmod 600, владелец root
export FINSPACE_BACKUP_KNOWN_HOSTS=/etc/finspace/known_hosts # host key закреплён заранее
export FINSPACE_BACKUP_REMOTE_LABEL=nas
sudo -E ./scripts/backup-offhost.sh <set_id>
```

Порядок: локальная сверка SHA-256 всех артефактов → staging-каталог → rsync в
`<remote_root>/finspace/sets/.<set_id>.partial` → `sha256sum -c SHA256SUMS` **на приёмнике** →
атомарное переименование в `<set_id>` → запись audit-события `backup.remote.copy` →
`offhost_verified=true` в отчёте.

Удалённый set самодостаточен: `database.dump`, `database.manifest.json`, `backup-set.json`,
`backup-set-report.json`, `SHA256SUMS` и, если включено, `n8n-data.tar.gz` + `n8n-data.sha256`.
Для восстановления эти файлы забираются локально и передаются существующему `restore.sh`.

Требования безопасности, которые скрипт проверяет **до** сетевых команд: непустой закреплённый
`known_hosts`, существующий ключ без прав для группы и остальных, `BatchMode=yes`,
`StrictHostKeyChecking=yes`, безопасный `set_id` и безопасный адрес приёмника. Существующий
финальный каталог на приёмнике не перезаписывается — это свидетельство прошлого удачного запуска.

**Поведение при отказе.** Прерванная или повреждённая передача не публикует финальный каталог, не
пишет `backup.remote.copy` и оставляет `offhost_verified=false`. Локальная копия при этом остаётся
проверенной и пригодной.

На приёмнике достаточно POSIX shell и `sha256sum`; демон Finspace там не нужен. Рекомендуется
отдельная учётная запись и ключ с ограничением в `authorized_keys`.

## Хранение секретов восстановления

**Автоматический backup не содержит и не должен содержать `.env`.** Секреты хранит оператор
отдельно: менеджер паролей или файл, зашифрованный `age`/`gpg`, на отдельном носителе. Finspace
намеренно не реализует управление ключами.

| секрет | что ломается при потере | восстановимость |
|---|---|---|
| `JWT_SECRET_KEY` | все сессии недействительны, пользователи входят заново | **финансовые данные не теряются**; ротация безопасна |
| `GOOGLE_TOKEN_ENCRYPTION_KEY` (+ `_VERSION`) | сохранённые Google-токены не расшифровать | требуется повторная авторизация во всех связках |
| `N8N_ENCRYPTION_KEY` | credentials n8n не расшифровать; сами workflow лежат в репозитории | credentials создаются заново |
| `POSTGRES_PASSWORD` | нет доступа к существующему тому | это учётные данные, а не ключ шифрования данных |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth не работает | выпускаются заново в Google Cloud |

Дамп базы **без** `GOOGLE_TOKEN_ENCRYPTION_KEY` восстанавливает всю финансовую историю, но не
Google-подключения. Это первое, что должен знать оператор.

## Что ещё не сделано в Stage A

Артефакты backup set и удалённые копии **пока не имеют собственной политики хранения и не
создаются по расписанию**. Планировщик, блокировка одновременных запусков, согласованная
retention для set и off-host-семантика в `/automation/backup/status` появляются в Stage B; до
этого set создаются и копируются вручную по командам выше.
