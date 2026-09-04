# Finspace: эксплуатационная инструкция

Инструкция рассчитана на владельца self-hosted Finspace и следующего разработчика. Она
не содержит секретных значений. Подробные технические контракты остаются в тематических
документах; здесь собраны повседневные действия и безопасные команды.

Установка с нуля — [production-install.md](production-install.md); переход между
релизами и откат — [upgrade.md](upgrade.md); учение по восстановлению в чистой среде —
[disaster-recovery-drill.md](disaster-recovery-drill.md). Этот документ описывает уже
установленный сервер.

Все production-команды Compose выполняются через `sudo`, потому что `.env` принадлежит
root: Compose читает его на хосте. `sudo -E` на этом сервере не поддерживается — если
команде нужна переменная, передавайте её явно, `sudo env VAR=value ...`.

## 1. Что где находится

| Объект | Расположение |
|---|---|
| Локальный Git | `C:\Users\Nikit\Documents\Finans` |
| Серверный Git | `/opt/finspace` |
| SSH | `ssh finspace` |
| Production UI | `https://terontyn-pc.tailfcdf00.ts.net/` |
| Backend для Apps Script | `https://terontyn-pc.tailfcdf00.ts.net:8443` |
| Локальный frontend на сервере | `http://127.0.0.1:3000` |
| Локальный backend на сервере | `http://127.0.0.1:8000` |
| Production Compose wrapper | `sudo finspace-compose`, ставится `sudo ./scripts/install-finspace-compose.sh` |
| Version-controlled production override | `/opt/finspace/compose.production.yml` |

Production UI доступен устройствам, вошедшим в нужный tailnet. Порт 8443 публичен только
для Apps Script Bridge. Не публикуйте PostgreSQL, Redis, Adminer или n8n.

### Production Google inventory

Эти идентификаторы не являются секретами, но относятся только к действующему production
контуру и не должны переноситься в runtime-код:

| Объект | Активное значение |
|---|---|
| Spreadsheet ID | `1Iw2lyS69SV6UAPsACmwu5kw-hCb4MpjhqEDryhh3j1s` |
| Bound Apps Script ID | `1tRKP3LLs7R16uws0KX6nwPk70JvbJYUXYPzL-GygdC0NkuZo12O_gDXD` |

Script ID `1Ldrqn4OW62A-iK3R-mPkvftMsCLnXshVtvV12LSBMqd94DevwXDpdkah` — старый
недоступный проект. Не выполняйте для него `clasp push`, deploy или изменение manifest.

## 2. Ежедневная работа

1. Откройте production UI и войдите в Finspace.
2. Добавляйте счета, категории и операции через приложение. PostgreSQL — источник истины.
3. Google Sheets можно использовать как дополнительный интерфейс. Triggers выполняют
   синхронизацию каждые 5 минут.
4. Не удаляйте финансовые строки физически из Google Sheets: такое удаление не удаляет
   запись PostgreSQL. Отмену/архивирование выполняйте через Finspace.
5. Если строка Sheets отмечена `DIRTY`/`PENDING`, дождитесь trigger или используйте меню
   **Финпространство → Отправить изменения**.
6. Для немедленного получения изменений приложения используйте
   **Финпространство → Получить обновления**.
7. Конфликты разрешайте в разделе Finspace **Конфликты**, не исправляйте скрытые UUID,
   version и hash вручную.

### Быстрая ежедневная проверка

- UI открывается;
- раздел **Google Sheets** показывает зарегистрированную таблицу и активный heartbeat;
- `Outbox ожидает`, `Ошибки доставки` и `Конфликты` не растут без объяснимой причины;
- последняя проверенная резервная копия не старше принятого владельцем интервала.

## 3. Проверка сервера

```bash
ssh finspace
cd /opt/finspace
git rev-parse --short HEAD
./scripts/git-status-strict.sh
sudo finspace-compose ps
tailscale serve status
tailscale funnel status
systemctl is-active cloudflared
```

Нормальное текущее состояние:

- frontend/backend/postgres/redis healthy;
- sync-worker запущен;
- Serve: frontend `127.0.0.1:3000` на HTTPS 443 внутри tailnet;
- Funnel: backend `127.0.0.1:8000` на публичном HTTPS 8443;
- cloudflared: `inactive`, автозапуск `disabled`;
- n8n запущен и healthy, остаётся локальным и изолированным от прямого доступа к
  PostgreSQL/Redis.

### Health без вывода данных

```bash
curl -fsS http://127.0.0.1:8000/api/v1/health
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
curl -fsS -o /dev/null -w 'frontend: HTTP %{http_code}\n' http://127.0.0.1:3000/login
curl -fsS -o /dev/null -w 'public backend: HTTP %{http_code}\n' \
  https://terontyn-pc.tailfcdf00.ts.net:8443/api/v1/health
```

Не используйте dump environment или `docker inspect` с полным `Config.Env` в отчётах.

## 4. Запуск после перезагрузки

Сначала проверьте состояние, затем поднимайте существующие сервисы без удаления volumes:

```bash
ssh finspace
cd /opt/finspace
sudo finspace-compose up -d
sudo finspace-compose ps
tailscale serve status
tailscale funnel status
```

Конфигурации Serve/Funnel, созданные с `--bg`, должны сохраняться через restart. Если
Funnel 8443 действительно отсутствует, после проверки локального backend восстановите
только этот маршрут:

```bash
sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8000
tailscale funnel status
```

Если отсутствует private frontend Serve, после проверки локального frontend восстановите:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:3000
tailscale serve status
```

Не выполняйте `tailscale serve reset`: он может удалить сразу всю Serve/Funnel
конфигурацию узла.

## 5. Логи и точечный restart

```bash
cd /opt/finspace
sudo finspace-compose logs --tail 100 frontend
sudo finspace-compose logs --tail 100 backend
sudo finspace-compose logs --tail 100 sync-worker
sudo finspace-compose logs --tail 100 categorization-prune
```

Restart выполняйте только для нужного сервиса:

```bash
sudo finspace-compose restart backend
sudo finspace-compose restart frontend
```

После backend restart проверьте health, readiness и Google heartbeat. После frontend
restart проверьте `/login` и восстановление сессии.

### 5.1 Проверка worker-процессов

Оба worker-процесса пишут те же JSON-логи, что и backend. Ни у одного нет порта и ни у
одного нет Docker healthcheck: `Up` подтверждает только то, что процесс жив, а
доказательством выполненной работы служат логи циклов.

```bash
sudo finspace-compose ps sync-worker
sudo finspace-compose logs --tail 100 sync-worker
sudo finspace-compose ps categorization-prune
sudo finspace-compose logs --tail 100 categorization-prune
```

Маркеры в логах `categorization-prune`:

| маркер | уровень | что означает |
|---|---|---|
| `categorization_prune_started` | INFO | процесс стартовал; в строке видны действующие `poll_seconds`, `batch_size`, `max_workspaces_per_cycle` |
| `categorization_prune_cycle_finished` | INFO | цикл отработал: `workspaces_examined`, `workspaces_failed`, `previews_deleted`, `duration_ms`, `next_cursor` |
| `categorization_prune_workspace_failed` | WARNING | один workspace не обработан; курсор идёт дальше, повтор на следующем обороте |
| `categorization_prune_cycle_failed` | ERROR | сбой цикла целиком; daemon не завершается, следующая попытка после обычной паузы |
| `categorization_prune_stopping` | INFO | штатное завершение по SIGTERM/SIGINT |
| `categorization_prune_disabled` | INFO | `CATEGORIZATION_PRUNE_ENABLED=false` |

Нормальная картина: одна строка `categorization_prune_started` на процесс и далее по
одной `categorization_prune_cycle_finished` каждые `CATEGORIZATION_PRUNE_POLL_SECONDS`
секунд (по умолчанию 900, то есть 15 минут). Повторяющийся `categorization_prune_started`
без `categorization_prune_stopping` между ними означает restart loop. Отсутствие
`categorization_prune_cycle_finished` дольше одного интервала при статусе `Up` означает
зависший или постоянно падающий цикл — смотрите `categorization_prune_cycle_failed`.

Для рутинной проверки здоровья worker-процессов достаточно `ps` и логов; запросы к
таблицам базы для этого не нужны.

## 6. Безопасный deploy

Deploy не должен быть первым способом диагностики. Сначала зафиксируйте симптом, commit,
status и логи. Не deploy-те dirty server worktree.

Production application code запускается из собранных Docker images. В итоговой
конфигурации backend и sync-worker не имеют bind mount исходников, backend не использует
`--reload`, frontend запускает `npm run start` и также не монтирует исходники или cache
volumes. Базовый `docker-compose.yml` остаётся development-конфигурацией и сам по себе на
production не используется.

### Перед изменением локально

```powershell
cd C:\Users\Nikit\Documents\Finans
git status --short
git diff --check
```

Существующие незнакомые изменения принадлежат пользователю: не удаляйте, не stash-ьте и
не перезаписывайте их без согласования.

### Обязательные проверки frontend

```powershell
cd C:\Users\Nikit\Documents\Finans\frontend
npm test
npm run typecheck
npm run lint
npm run build
```

### Обязательные проверки backend

Если backend изменён, используйте изолированный runner и quality checks:

```powershell
cd C:\Users\Nikit\Documents\Finans
docker compose exec -e TESTING=true backend python scripts/test_runner.py
docker compose exec backend ruff check .
docker compose exec backend ruff format --check app alembic scripts tests
docker compose exec backend mypy app
```

Прямой `pytest` не является штатным способом: test runner создаёт отдельную маркированную
БД и удаляет только её. Ограниченный список для format-check намеренно исключает
read-only backup/data mounts, которые на Docker Desktop могут возвращать `Permission
denied`, но не содержат исходного Python-кода.

### Проверка Compose перед deploy

Production topology требует Docker Compose 2.24.4+, потому что
`compose.production.yml` использует `!override`. До будущего deploy validator обязан
завершиться с PASS.

Локально проверяются оба режима без вывода полного rendered config:

```bash
python3 backend/scripts/validate_compose_topology.py all
```

На сервере проверяется именно merged config production wrapper:

```bash
sudo finspace-compose config --quiet
sudo finspace-compose config --format json |
  python3 backend/scripts/validate_compose_topology.py production --stdin
```

Validator печатает только PASS/FAIL и не выводит environment. Полный `compose config` не
прикладывайте к отчёту: он может содержать secrets после интерполяции.

### Writable runtime storage

Backend image намеренно запускает `app` с числовым контрактом из
`backend/runtime-identity.env` (сейчас UID `100`, GID `101`). Перед первым запуском на
новом host и после изменения этого контракта root должен подготовить только утверждённые
read-write bind mounts:

| Host path | Runtime writer |
|---|---|
| `/opt/finspace/data/imports` | Backend API: загрузка и удаление staging-файлов импорта |
| `/opt/finspace/data/acceptance` | Backend live-acceptance tooling: registry |
| `/opt/finspace/backups/acceptance-reports` | Backend live-acceptance tooling: отчёты |

```bash
cd /opt/finspace
sudo ./scripts/prepare-runtime-storage.sh /opt/finspace
```

Команда идемпотентно создаёт отсутствующие каталоги. Owner берётся из владельца
`/opt/finspace`, group — runtime GID `101`, mode — `2770` с setgid. Владелец checkout
сохраняет доступ к tracked `.gitkeep` и обычным Git-операциям, backend UID `100` пишет
через свою primary group `101`, world-access отсутствует. `chown` не рекурсивный:
существующие файлы, остальные backup, PostgreSQL, Redis и n8n data не затрагиваются.
`/app/backups`, Apps Script package и n8n package остаются read-only для backend;
production sync-worker не имеет filesystem mounts.

`git status` способен завершиться с кодом 0 одновременно с permission diagnostics в
stderr. Поэтому server runbook использует `./scripts/git-status-strict.sh`: helper
печатает обычный short status, но завершает release gate ошибкой при любом Git stderr.
При одноразовом переходе с прежнего контракта `100:101/0750` сначала выберите точный
release с этим исправлением, затем немедленно запустите storage preparation и только
после неё strict status. Tracked `.gitkeep` не удаляются, поэтому переход не требует
записи Git в закрытые runtime-каталоги.

### Полный deploy на Ubuntu

Канонический порядок обновления и все три сценария отката вынесены в отдельный документ:
**[upgrade.md](upgrade.md)**. Он и есть контракт; здесь не дублируется его команда за
командой, чтобы две последовательности не разошлись.

Commit/push и deploy выполняются только после разрешения владельца. `release_ref` должен
быть точным проверенным tag или commit, а не плавающей веткой.

Инварианты, которые нельзя нарушать ни в одном варианте deploy:

1. Свежая копия, **проверенная восстановлением**, создаётся до обновления, а не после.
2. Точный release выбирается до сборки; код приложения и код миграций принадлежат одному
   checkout.
3. Production-оверлей берётся из этого же checkout через `finspace-compose`. Внешний
   `/etc/finspace/compose.server.yml` больше не участвует в процедуре: wrapper
   устанавливается из репозитория командой `sudo ./scripts/install-finspace-compose.sh`.
4. Merged Compose проходит validator до сборки.
5. Migration выполняется образом целевого релиза, до запуска приложения.
6. Пересоздаются только нужные application services; PostgreSQL, Redis и n8n остаются.
7. Никакого автоматического downgrade production DB. `alembic downgrade` не является
   механизмом отката.

Нельзя выполнять `git pull` поверх работающего source-mounted backend: изменение checkout
немедленно меняет исполняемый код и обходит build/restart boundary. В корректной
production-топологии source mounts отсутствуют, но application services всё равно
останавливаются до смены release, чтобы порядок deploy оставался однозначным.

### Frontend-only release

Для доказанно frontend-only изменения допустимо остановить только frontend, но до build
всё равно нужно выбрать точный release, установить/проверить production override и
подтвердить отсутствие mounts. После запуска проверьте `/login`, hydration и отсутствие
`/_next/webpack-hmr`.

Подробности production frontend: [frontend-production.md](frontend-production.md).

## 7. Backup и восстановление

### Создать backup

```bash
cd /opt/finspace
sudo finspace-compose --profile tools run --rm backup sh /scripts/backup.sh
```

### Создать и проверить восстановлением

```bash
sudo finspace-compose --profile tools run --rm backup \
  sh /scripts/verify-backup.sh --create
```

Проверка должна:

- подтвердить SHA-256 manifest и custom format;
- восстановить dump в отдельную временную БД;
- сверить Alembic revision и основные таблицы;
- удалить только временную БД.

### Плановые backup (systemd)

Backup выполняет host systemd, а не n8n и не контейнер:

```bash
systemctl list-timers finspace-backup.timer
sudo systemctl start finspace-backup.service
journalctl -u finspace-backup.service -n 200 --no-pager
```

Ежедневно в 01:00 **по локальному времени хоста**, с наверстыванием пропуска и разбросом старта.
Одновременные запуски исключены `flock`: второй пишет `backup_run_locked lock=busy` и завершается
ошибкой.

Признак нормального прогона в журнале: `backup_run_started` → `backup_run_created` →
`backup_run_local_verified` → `backup_run_offhost_verified` (или `backup_run_offhost_skipped` в
временном режиме local-only) → `backup_run_retention_finished` → `backup_run_finished`.

Пока внешнее хранилище Homelab не построено, в `/etc/finspace/backup.env` допустим
`FINSPACE_BACKUP_OFFHOST_ENABLED=false`. Тогда `/api/v1/automation/backup/status` штатно отвечает
`unverified` («не подтверждена вне этого хоста»), и это ожидаемо: копия только на этом сервере не
является защитой от потери сервера. Подробности и порядок включения внешней копии —
[backup-and-restore.md](backup-and-restore.md).

Рабочую БД не восстанавливайте «поверх» без отдельного плана, проверенного dump и точного
подтверждения владельца. Локальный backup не защищает от потери всего сервера: нужна
внешняя зашифрованная копия и отдельно сохранённые ключи.

Полный порядок: [backup-and-restore.md](backup-and-restore.md).

## 8. Импорт выписки

Finspace принимает CSV и XLSX через staging:

1. В разделе **Импорт** загрузите файл.
2. Сопоставьте исходные столбцы.
3. Запустите проверку строк.
4. Исправьте неизвестные счета/категории и ошибки.
5. Проверьте дубликаты.
6. Только затем подтвердите commit.

Upload и validation не создают финансовые операции. Rollback batch не должен удалять
ручные операции; изменённые после импорта записи требуют явного решения конфликта.

Никогда не загружайте macro-enabled workbook и не преобразуйте банковский логин/пароль в
«интеграцию». Подробнее: [import.md](import.md).

### Staging-файлы импорта

`./data/imports` — это **не** резервная копия: файл разбирается один раз при загрузке, а
дальше истина в PostgreSQL. Штатно он удаляется сразу при commit, cancel или ошибке
загрузки, поэтому каталог не растёт. Остатки появляются только после аварийной остановки.

Осмотр без удаления (режим по умолчанию, ничего не трогает):

```bash
cd /opt/finspace
sudo finspace-compose run --rm --no-deps backend python scripts/import_staging_reclaim.py
```

Отчёт показывает число файлов и байт, сколько подлежит освобождению и почему остальное
пропущено. Удаление выполняется явно:

```bash
sudo finspace-compose run --rm --no-deps backend python scripts/import_staging_reclaim.py --apply
```

Незавершённый импорт не удаляется никогда, независимо от возраста. Уборка не трогает
`import_batches`, `import_rows`, операции и любую другую строку БД. Классы, grace-период,
необязательный weekly-таймер и поведение при отказе: [import.md](import.md#освобождение-staging-reclamation).

### 8.1. Закрытие месяца

Month Close является hard accounting close. После confirm любые операции, исторические
изменения счетов, import rollback/commit и входящие integration writes с датой не позднее
`closed_through` получают `409 MONTH_CLOSED`. Не пытайтесь обходить это через Google
Sheets, Telegram, n8n или прямой SQL.

Безопасный порядок:

1. Выполнить prepare только для завершённого месяца.
2. Проверить три отдельные группы issues: blockers нужно устранить, warnings осознанно
   принять, info не требует исправления. Отсутствие reconciliation evidence — warning.
3. Проверить coverage только для счетов с активностью или ненулевым period-end balance;
   более поздняя statement date может корректно покрывать конец месяца.
4. Проверить честный backup status. При `warn` missing/unverified/stale не запускают
   backup автоматически и не блокируют confirm; при `require_healthy` confirm запрещён.
5. Owner открывает confirm modal, сверяет totals отдельно по валютам, warning count,
   сокращённый fingerprint и cutoff, затем подтверждает актуальный preview.
6. Для поздней корректировки owner вручную открывает только последний closed month и
   обязательно указывает причину. Более ранние периоды открываются в обратном порядке.
7. После исправлений снова выполнить prepare/confirm для каждого месяца по порядку.

Viewer может читать periods, immutable history и as-closed reports. Editor может также
выполнять prepare. Только owner видит и выполняет confirm/reopen; backend всё равно
повторно проверяет permissions. В history `Legacy unverified` означает отсутствие
доказуемого fingerprint, а не повреждение записи. В drawer не путайте «Текущие данные» с
«Закрыто в revision N»: первое перечитывает live ledger, второе всегда берётся из
immutable snapshot.

Никогда не используйте «новый» idempotency key для слепого повтора после сетевого сбоя:
повторите тот же confirm/reopen с тем же ключом и payload. Reconciliation уже закрытой
операции разрешён и не требует reopen. Полный контракт: [Hard Month Close](month-close.md).

## 8.1. Проверка цепочки миграций

Воспроизводимый гейт: граф миграций плюс прогон на **изолированной временной базе**.
Production-базу он не трогает — создаёт и удаляет только собственные `finspace_test_<uuid>`
и никогда не выполняет `alembic downgrade`.

```bash
cd /opt/finspace
# Полный гейт: граф + чистая база + исторические контрольные точки.
sudo finspace-compose run --rm --no-deps -e TESTING=true backend \
  python scripts/validate_migrations.py \
  --expect-head 0017_categorization_history --expect-count 17

# Только граф, без базы (быстро, ничего не создаёт).
sudo finspace-compose run --rm --no-deps backend \
  python scripts/validate_migrations.py --static-only

# Что стоит в самой production-базе — только чтение.
sudo finspace-compose run --rm --no-deps backend alembic current
```

Порядок обновления сервера от этого не меняется: [upgrade.md](upgrade.md).

## 8.1.1. Performance smoke

Воспроизводимый гейт против очевидных регрессий производительности на повседневных путях
чтения. Работает на **собственной временной базе** с синтетическими данными: production-базу
не читает, ничего в неё не пишет и не измеряет реальные финансовые данные.

```bash
cd /opt/finspace
sudo finspace-compose run --rm --no-deps -e TESTING=true backend \
  python scripts/performance_smoke.py

# Машинно-читаемое свидетельство для приёмки релиза.
sudo finspace-compose run --rm --no-deps -e TESTING=true backend \
  python scripts/performance_smoke.py --json
```

Что проверяется: число SQL-запросов на сценарий, отсутствие роста этого числа при
десятикратном увеличении объёма данных, соблюдение лимитов страниц и грубый порог
катастрофы (15 000 мс). Значения `duration_ms` — **наблюдения запуска, а не SLA**: на другом
железе они будут другими, и по ним ничего не блокируется.

> [!NOTE]
> **Известный дефект, зафиксированный этим гейтом.** Страница операций стоит примерно
> **3,3 SQL-запроса на возвращённую строку**: `transaction_response` делает отдельный запрос
> splits на каждую операцию и догружает её счёт и категорию по одному. При странице в 200
> строк это ~625 запросов. Число запросов **не растёт** с объёмом истории — только с размером
> страницы, поэтому на домашних объёмах это не блокер. Гейт помечает такие сценарии как
> `known defect` и не даст ситуации ухудшиться; исправление — отдельная задача, и вместе с
> ним нужно снизить границу `TRANSACTION_PAGE_QUERIES_PER_ROW`.

## 8.2. Что занимает место

Полная опись доменов, их владельцев retention и запретов —
[data-lifecycle.md](data-lifecycle.md). Все команды ниже **только читают**: режима удаления у
них нет.

```bash
# Хост целиком: файловая система + PostgreSQL + staging импорта.
sudo /opt/finspace/scripts/data-lifecycle-report.sh

# Только PostgreSQL, человеко-читаемо и машинно-читаемо.
cd /opt/finspace
sudo finspace-compose run --rm --no-deps backend python scripts/data_lifecycle_report.py
sudo finspace-compose run --rm --no-deps backend python scripts/data_lifecycle_report.py --json

# Staging импорта отдельно (F010, только осмотр).
sudo finspace-compose run --rm --no-deps backend python scripts/import_staging_reclaim.py

# Место на диске и размеры томов.
df -h /opt/finspace /var/lib/docker
sudo du -sh /opt/finspace/backups/database /opt/finspace/backups/sets
sudo docker system df -v | head -30
```

Снимок для сравнения во времени:

```bash
sudo finspace-compose run --rm --no-deps backend python scripts/data_lifecycle_report.py --json \
  > "/opt/finspace/data/acceptance/data-lifecycle-$(date -u +%Y-%m-%dT%H%M%SZ).json"
```

Куда смотреть, если растёт:

| домен | владелец | что делать |
|---|---|---|
| финансовые таблицы | только действие пользователя | ничего; это работа приложения |
| `audit_log`, `auth_sessions` | никакого сегодня | наблюдать; политика удержания — отдельное решение |
| `categorization_previews` | worker `categorization-prune` | проверить `categorization_prune_cycle_finished` в логах |
| `data/imports` | F010 | осмотр, затем `--apply` |
| `backups/database`, `backups/sets` | `backup-cleanup.sh`, `backup-set-cleanup.sh` | менять `BACKUP_RETENTION_*`, не удалять руками |
| `data/acceptance`, `backups/acceptance-reports` | оператор | архивировать вручную |
| логи Docker, journald | **хост** | `daemon.json` `log-opts`, `journald.conf` |
| незнакомая таблица | никто | классифицировать в `data_lifecycle.py` |

> [!CAUTION]
> Никогда: `docker system prune -a`, `docker volume prune`, `docker compose down -v`, ручное
> удаление содержимого `finspace_postgres_data`, дампов, backup sets или строк `audit_log`.

## 9. Google Sheets Bridge

### Обычные действия в книге

- **Настроить подключение** — ввод публичного backend URL, Binding ID и одноразового
  secret в Document Properties.
- **Установить триггеры** — installable onEdit, schedule и onOpen.
- **Отправить изменения** — обработать локальную очередь Sheets → Backend.
- **Получить обновления** — pull/ACK Backend → Sheets.
- **Полная сверка** — сравнить canonical snapshot; не равна «затереть таблицу».
- **Статус подключения** — проверить регистрацию, heartbeat и последние операции.

### Проверка конфигурации backend

```bash
cd /opt/finspace
docker exec finspace-backend-1 python scripts/google_config_check.py
```

Команда должна печатать только PASS/FAIL и не выводить secret values.

### Если heartbeat устарел

1. Проверьте backend health локально и через Funnel.
2. Проверьте `tailscale funnel status`.
3. Выполните config check.
4. В Apps Script выполните тестовый `UrlFetchApp.fetch` только к health endpoint.
5. Проверьте, что в книге ровно нужные triggers и они не дублируются.
6. Выполните **Статус подключения**, затем **Получить обновления**.

Не создавайте новый binding до диагностики: это без необходимости инвалидирует старый
secret. Secret не записывайте в cells и не отправляйте ассистенту.

### Apps Script package

Файлы: `Code.gs`, `Config.gs`, `EditQueue.gs`, `SyncClient.gs`, `Template.gs`,
`Validation.gs`, настоящий manifest `appsscript.json`. Manifest включается в Apps Script
через **Настройки проекта → Показывать файл манифеста**; его нельзя создавать как
`appsscript.json.gs`.

## 10. n8n и Telegram

Production n8n запущен и healthy на `127.0.0.1:5678`. Он остаётся изолированным от прямого
доступа к PostgreSQL/Redis и работает только через ограниченный Backend Automation API.
Обычный Finspace application deploy не должен останавливать, пересоздавать или менять n8n,
если release не содержит n8n-specific changes.

Сохраняются обязательные инварианты:

1. создан настоящий случайный `N8N_ENCRYPTION_KEY` и сохранён вне Git;
2. продуман отдельный backup volume `finspace_n8n_data` вместе с этим ключом;
3. в Finspace создан service account с минимальными permissions;
4. одноразовый ServiceKey сохранён в encrypted n8n credential;
5. Telegram bot token сохранён только в encrypted credential;
6. workflows импортированы, credentials назначены и каждый workflow вручную проверен;
7. n8n остаётся на `127.0.0.1:5678` и не публикуется через Funnel/Cloudflare.

Для n8n-specific изменений используются инструкции [n8n.md](n8n.md),
[Telegram](telegram.md) и
[automation security](automation-security.md).

## 11. Типовые неисправности

### «Восстанавливаем защищённую сессию…» не исчезает

- Проверьте, что production frontend действительно запускает `next start`, а не
  `next dev`.
- В браузере не должно быть запроса `/_next/webpack-hmr`.
- Проверьте загрузку JS chunks и Console.
- Session restore имеет timeout 10 секунд; после него приложение должно перейти на
  `/login`, а не зависнуть.
- Не меняйте auth code до воспроизводимого теста в React StrictMode.

### API request получает status 0 / `ERR_BLOCKED_BY_CLIENT`

- Проверьте реальный Request URL в DevTools Network.
- Правильный browser URL начинается с текущего frontend origin и `/api/v1/...`, а не с
  `https://api/...` и не с backend 8443.
- Повторите в чистом Edge/Chromium profile без extensions.
- Проверьте Yandex Protect, ad blocker, antivirus web shield и DNS filter.
- Обычный GET к POST endpoint и ожидаемый 405 не проверяют отправку формы.

### Frontend показывает только server-rendered loading HTML

- Это признак отсутствия hydration/JS chunks.
- Проверьте production image, health и отсутствие dev HMR.
- Пересоберите только frontend; не удаляйте PostgreSQL/Redis volumes.

### Google Sheets показывает «Bridge не готов»

- Запустите `google_config_check.py`.
- Проверьте provider, bridge enabled, public backend URL и Redis только через безопасный
  check, не печатая environment.
- Проверьте Funnel health из Apps Script, а не только из домашней сети.

### Cloudflare Tunnel Down/Degraded

Текущий production flow Cloudflare не использует. `cloudflared` должен оставаться
inactive/disabled, а Apps Script — использовать Tailscale Funnel 8443. Не запускайте два
конкурирующих tunnel-варианта без отдельного плана.

## 12. Запрещённые быстрые решения

- `docker compose down -v`, `docker volume rm`, `git reset --hard`;
- удаление или пересоздание PostgreSQL volume для «исправления» приложения;
- чтение/печать полного `.env` или `/etc/finspace/finspace.env`;
- передача секретов через build args, Git, чаты, screenshots или workflow export;
- запуск тестов против `finspace`/production DB;
- прямой SQL из n8n, Apps Script или будущего bank connector;
- публикация n8n, Adminer, PostgreSQL или Redis через Funnel;
- физическое удаление строк Sheets как способ удаления финансовых данных;
- commit, push или deploy без явно согласованного шага.

## 13. Передача работы следующему ассистенту

Передайте ему [project-history-and-status.md](project-history-and-status.md), этот runbook
и готовый [handoff prompt](next-assistant-handoff.md). Ассистент обязан проверить текущее
состояние, а не считать снимок 2026-08-06 вечным.
