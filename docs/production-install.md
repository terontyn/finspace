# Production-установка Finspace на чистый сервер

Этот документ — канонический путь установки. Он рассчитан на чистый Linux-сервер и на
оператора, у которого нет доступа к истории проекта: всё, что нужно знать, находится
здесь и в документах, на которые он ссылается.

Повседневные команды описаны в [эксплуатационной инструкции](operations-runbook.md),
переход между релизами — в [upgrade.md](upgrade.md).

> [!IMPORTANT]
> Базовый `docker-compose.yml` — это конфигурация разработки: source mounts, backend с
> `--reload`, frontend в dev-режиме. **Одного `docker compose up` недостаточно и он не
> является production-установкой.** Production всегда объединяет базовый файл с
> `compose.production.yml`, и делает это ровно одна команда — `finspace-compose`.

## 0. Что даёт эта установка и чего она не даёт

Даёт: работающее приложение из собранных images, миграции ровно того релиза, который
выбран, ежедневный проверенный backup по расписанию и понятный порядок обновления.

Не даёт: внешний ingress (reverse proxy, Tailscale, DNS, сертификаты) и физически
отдельное хранилище резервных копий. И то и другое — внешняя инфраструктура; см.
разделы [11](#11-внешний-доступ-reverse-proxy-и-tailscale) и
[9.4](#94-временный-режим-local-only).

## 1. Предварительные требования

| Требование | Зачем |
|---|---|
| Linux-хост с systemd | планировщик backup — host systemd unit |
| Docker Engine | все сервисы |
| Docker Compose plugin **2.24.4 и новее** | `compose.production.yml` использует `!override` |
| Git | выбор точного релиза, release metadata в backup set |
| `python3` | `backend/scripts/validate_compose_topology.py` |
| `curl` | health-проверки |
| `util-linux`: `flock`, `setpriv` | блокировка запусков backup, проверка runtime-доступа |
| `rsync` и `openssh-client` | внешняя копия backup на отдельный хост |
| Учётная запись оператора с `sudo` | всё, что читает защищённый `.env` |

Дисковое пространство. Ориентир, а не гарантия: место под PostgreSQL volume, плюс
примерно двукратный размер БД на dump и его проверку восстановлением (проверка поднимает
временную БД), плюс место под удерживаемые копии — по умолчанию 7 ежедневных и 4
еженедельных.

Пример для Debian/Ubuntu — **именно пример**, а не часть контракта; на другом дистрибутиве
поставьте те же компоненты его средствами:

```bash
sudo apt-get update
sudo apt-get install -y git curl python3 util-linux rsync openssh-client
# Docker Engine и Compose plugin ставятся по официальной инструкции Docker для дистрибутива.
docker compose version --short
```

`docker compose version --short` должен вернуть 2.24.4 или больше.

## 2. Каноническая раскладка каталогов

```
/opt/finspace                     # Git checkout, владелец — оператор
/opt/finspace/.env                # секреты приложения, root:root 0600
/opt/finspace/backups             # dump, manifests, backup sets
/opt/finspace/data/imports        # staging-файлы импорта, пишет backend
/opt/finspace/data/acceptance     # registry live-acceptance, пишет backend
/etc/finspace                     # host-only конфигурация, root:root 0700
/etc/finspace/backup.env          # конфигурация планировщика, root:root 0600
/etc/systemd/system/finspace-backup.service
/etc/systemd/system/finspace-backup.timer
/usr/local/bin/finspace-compose   # production Compose wrapper, root:root 0755
```

Обязательные свойства:

- `.env` **никогда не попадает в Git** — он в `.gitignore`, и его там нужно оставить;
- `/etc/finspace/backup.env` принадлежит root и не содержит секретов приложения;
- каталог dump-ов (`backups/database`) остаётся закрытым: `0700`, артефакты `0600
  root:root`. Backend не должен и не обязан их читать — метаданные последнего backup он
  получает из audit-события, а не из файловой системы;
- `chmod 755` на каталоге backup, `chmod 644` на manifests, добавление backend в
  «backup-группу» — **запрещены**. Это не обход неудобства, это отмена изоляции.

## 3. Учётная запись и checkout

```bash
sudo install -d -m 0755 -o "$USER" -g "$USER" /opt/finspace
git clone https://github.com/terontyn/finspace.git /opt/finspace
cd /opt/finspace
```

Если сервер ходит в Git по SSH, ключ создаётся на сервере, а публичная часть добавляется
как deploy key репозитория. Приватный ключ не копируется с рабочей машины и никогда не
попадает в репозиторий. Установка не предполагает интерактивного ввода учётных данных.

### Выбор точного релиза

Production работает с **точным тегом**, а не с плавающей веткой:

```bash
git fetch --tags origin
release_ref="EXACT_TAG"          # например local-v0.15
git switch --detach "$release_ref"
git rev-parse HEAD
./scripts/git-status-strict.sh
```

`git-status-strict.sh` печатает обычный короткий status и завершается ошибкой при любой
диагностике Git в stderr: `git status` умеет вернуть 0 и одновременно пожаловаться на
права.

## 4. Конфигурация: `.env`

```bash
sudo install -o root -g root -m 0600 /opt/finspace/.env.example /opt/finspace/.env
sudoedit /opt/finspace/.env
```

`.env` принадлежит root с режимом `0600`. Прямое следствие, о котором нужно помнить весь
остальной документ: **обычный оператор не может прочитать `.env`, поэтому любая команда
Compose выполняется через `sudo`** — Compose читает `.env` на хосте, и для интерполяции
переменных, и для `env_file`.

### 4.1 Обязательные секреты

Генерируются на сервере, сохраняются во внешнем менеджере паролей и **не** коммитятся:

| Переменная | Когда обязательна |
|---|---|
| `POSTGRES_PASSWORD` | всегда |
| `JWT_SECRET_KEY` | всегда |
| `N8N_ENCRYPTION_KEY` | если используется n8n |
| `GOOGLE_TOKEN_ENCRYPTION_KEY` и `GOOGLE_TOKEN_ENCRYPTION_KEY_VERSION` | если используется Google OAuth provider |
| `GOOGLE_CLIENT_ID` и `GOOGLE_CLIENT_SECRET` | если используется Google OAuth provider |

```bash
# Длинное случайное значение; повторите для каждого секрета отдельно.
head -c 32 /dev/urandom | base64
```

Что ломается при потере каждого из них — таблица в
[backup-and-restore.md](backup-and-restore.md#хранение-секретов-восстановления). Коротко:
дамп БД без `GOOGLE_TOKEN_ENCRYPTION_KEY` восстанавливает всю финансовую историю, но не
Google-подключения.

### 4.2 Обязательная конфигурация

`DATABASE_URL`, `TEST_DATABASE_URL`, `REDIS_URL`, `POSTGRES_DB`, `POSTGRES_USER`,
`CORS_ORIGINS` обязательны: Compose останавливается, если их нет. Для production также
приведите к рабочим значениям:

```env
ENVIRONMENT=production
DEBUG=false
TESTING=false
ALLOW_DEV_AUTH_HEADERS=false
AUTH_COOKIE_SECURE=true
CORS_ORIGINS=https://<ваш-frontend-origin>
PUBLIC_BACKEND_URL=https://<публичный-backend-origin>
INTERNAL_API_URL=http://backend:8000
NEXT_PUBLIC_API_URL=/
```

`AUTH_COOKIE_SECURE=true` требует, чтобы UI открывался по HTTPS: cookie обновления сессии
иначе не будет отправлена браузером. `ALLOW_REGISTRATION` оставьте `true` только до
создания владельца, затем осознанно решите.

`DATABASE_URL` и `TEST_DATABASE_URL` должны содержать тот же пароль, что и
`POSTGRES_PASSWORD`, иначе backend не подключится к уже созданному volume.

### 4.3 Необязательные интеграции

Google Sheets через Apps Script Bridge (`GOOGLE_SYNC_PROVIDER=apps_script_bridge`,
`APPS_SCRIPT_BRIDGE_ENABLED=true`), Google Cloud OAuth (по умолчанию выключен), n8n и
Telegram. Ни одна из них не требуется для работы финансового ядра и ни одна не входит в
контур восстановления данных.

Настройки внешней копии backup (`FINSPACE_BACKUP_*`) в `.env` **не попадают**: они живут
в `/etc/finspace/backup.env` и не должны быть видны контейнерам приложения.

## 5. Production Compose wrapper

`finspace-compose` — единственная поддерживаемая точка входа в production Compose. Он
объединяет базовый файл и production-оверлей в фиксированном порядке, с явным project
directory:

```bash
cd /opt/finspace
sudo ./scripts/install-finspace-compose.sh
```

Скрипт кладёт `scripts/finspace-compose.sh` в `/usr/local/bin/finspace-compose`
(`root:root`, `0755`) и проверяет, что установленная копия побайтово совпадает с
репозиторием. **Та же команда выполняет обновление wrapper** — отдельной процедуры нет.
Установить в другое место можно через `FINSPACE_COMPOSE_BIN`.

Что делает wrapper и чего не делает:

```
docker compose --project-directory /opt/finspace \
  --file /opt/finspace/docker-compose.yml \
  --file /opt/finspace/compose.production.yml \
  <ваши аргументы без изменений>
```

- порядок файлов фиксирован: оверлей применяется поверх базового;
- аргументы передаются дословно, `eval` не используется, командная строка не собирается
  из строки;
- код возврата — это код возврата `docker compose`; собственные ошибки wrapper (нет
  checkout, нет compose-файла, нет `docker`) дают код `2`;
- секретов в wrapper нет: он лишь указывает Compose, где искать `.env`.

> [!IMPORTANT]
> **Внешний `/etc/finspace/compose.server.yml` больше не является контрактом.**
> Production-оверлей берётся прямо из checkout, поэтому выбор релиза автоматически
> означает выбор топологии и вторая копия оверлея не может «отстать». На сервере, где
> этот файл остался с прежних времён, достаточно установить wrapper из репозитория и
> убедиться, что merged-конфигурация проходит validator (раздел 6). Сам файл ничего
> больше не читает; удалять его необязательно, но он не должен упоминаться ни в одной
> процедуре.

### 5.1 Контракт sudo

Production-политика sudo **не поддерживает `sudo -E`**: окружение вызывающего сбрасывается
и переменные до Compose не доходят. Проверено на этом сервере. Поэтому:

```bash
# Нельзя:
sudo -E finspace-compose ...

# Нужно, когда переменная действительно требуется:
sudo env FINSPACE_COMMIT="$COMMIT" FINSPACE_TAG="$TAG" finspace-compose ...
```

Передавайте только те переменные, которые нужны команде, а не всё окружение. Практическое
следствие для wrapper: `FINSPACE_PROJECT_ROOT` под `sudo` не наследуется, поэтому
`sudo finspace-compose` всегда работает с `/opt/finspace`. Это и есть желаемое поведение.

Плановому backup явная передача не нужна: `scripts/backup-run.sh` сам читает commit и tag
из checkout.

## 6. Проверка топологии до первого запуска

Выполняется **до** первого старта и **до** каждого обновления:

```bash
cd /opt/finspace
sudo finspace-compose config --quiet
sudo finspace-compose config --format json |
  python3 backend/scripts/validate_compose_topology.py production --stdin
```

Первая команда доказывает, что merged-конфигурация вообще собирается и все обязательные
переменные заданы. Вторая — что это действительно production-топология: нет source mounts
у backend, worker и frontend, нет `--reload`, frontend запускается `npm run start` из
production-target, а список mounts backend совпадает с утверждённым.

Validator печатает только PASS/FAIL. **Полный вывод `compose config` в отчёты не
прикладывается**: после интерполяции он содержит секреты. Именно поэтому проверка
устроена как pipe в validator, а не как чтение конфигурации глазами.

Проверить оба режима из repository-файлов (нужен доступ к `.env`, то есть root):

```bash
sudo python3 backend/scripts/validate_compose_topology.py all
```

## 7. Runtime-каталоги

Backend работает под числовым контрактом из `backend/runtime-identity.env` (сейчас UID
`100`, GID `101`) и пишет ровно в три bind mount. Их готовит root, идемпотентно:

```bash
cd /opt/finspace
sudo ./scripts/prepare-runtime-storage.sh /opt/finspace
```

Скрипт создаёт `data/imports`, `data/acceptance` и `backups/acceptance-reports`, ставит
владельца checkout, группу `101` и режим `2770` с setgid. Владелец репозитория сохраняет
Git-доступ, backend пишет через свою primary group, world-доступа нет. `chown` не
рекурсивный: существующие файлы, остальные backup, PostgreSQL, Redis и n8n data не
затрагиваются. `/app/backups`, Apps Script package и n8n package остаются read-only для
backend.

## 8. Первый запуск

Порядок детерминирован: сначала хранилище состояния, затем миграции из образа нужного
релиза, затем приложение.

```bash
cd /opt/finspace

# 1. Собрать images выбранного релиза.
sudo finspace-compose build backend sync-worker categorization-prune frontend

# 2. Поднять только состояние.
sudo finspace-compose up -d postgres redis
sudo finspace-compose ps

# 3. Миграции — из образа этого же релиза, без запуска приложения.
sudo finspace-compose run --rm --no-deps backend alembic upgrade head
sudo finspace-compose run --rm --no-deps backend alembic current

# 4. Приложение.
sudo finspace-compose up -d backend
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
sudo finspace-compose up -d sync-worker categorization-prune frontend
sudo finspace-compose ps
```

`alembic current` на этом релизе должен показать его head. На момент написания документа
head — `0017_categorization_history`; сверяйтесь с выбранным релизом, а не с этой цифрой.

> [!WARNING]
> Код приложения и код миграций обязаны принадлежать **одному и тому же checkout**.
> Никогда не запускайте `alembic upgrade` из другого релиза, чем тот, который затем
> обслуживает базу, и не запускайте миграции «заранее», до сборки образа этого релиза.

Первый пользователь создаётся через UI: откройте frontend и зарегистрируйте владельца.
Bootstrap не обходит аутентификацию и вне development недоступен.

## 9. Плановые проверенные backup

Планировщик — host systemd, а не n8n и не контейнер: backup не должен зависеть от
опционального сервиса автоматизации и должен переживать пересоздание контейнеров.

### 9.1 Установка units

```bash
cd /opt/finspace
sudo install -m 0644 infrastructure/systemd/finspace-backup.service /etc/systemd/system/
sudo install -m 0644 infrastructure/systemd/finspace-backup.timer   /etc/systemd/system/
sudo install -d -m 0700 -o root -g root /etc/finspace
sudo install -m 0600 -o root -g root /dev/null /etc/finspace/backup.env
sudo systemctl daemon-reload
```

### 9.2 `/etc/finspace/backup.env`

Host-only конфигурация. Секретов приложения здесь быть не должно: ни `JWT_SECRET_KEY`, ни
`GOOGLE_TOKEN_ENCRYPTION_KEY`, ни `N8N_ENCRYPTION_KEY`, ни `POSTGRES_PASSWORD`, ни
`GOOGLE_CLIENT_SECRET`.

```env
FINSPACE_PROJECT_ROOT=/opt/finspace
FINSPACE_BACKUP_ROOT=/opt/finspace/backups
FINSPACE_COMPOSE=finspace-compose
FINSPACE_BACKUP_OFFHOST_ENABLED=false
```

Значения для внешнего хранилища добавляются, когда оно появится (раздел 9.4).

`FINSPACE_COMPOSE` указан голым именем намеренно: оно разрешается через `PATH` и не устаревает
при переносе wrapper. Если вы всё же пишете абсолютный путь, это должен быть ровно
`/usr/local/bin/finspace-compose`, иначе плановый backup сломается молча. Проверяет это
`sudo ./scripts/check-backup-env-wrapper.sh`, и его же вызывает установщик wrapper.

### 9.3 Включение и первый прогон

```bash
sudo systemctl enable --now finspace-backup.timer
systemctl is-enabled finspace-backup.timer
systemctl is-active finspace-backup.timer
systemctl list-timers finspace-backup.timer

sudo systemctl start finspace-backup.service
journalctl -u finspace-backup.service -n 200 --no-pager
```

Расписание по умолчанию: ежедневно в 01:00 **по локальному времени хоста** (не UTC), с
`Persistent=true` (пропущенный запуск наверстывается) и `RandomizedDelaySec=15min`. Это
согласовано с `BACKUP_STALE_HOURS=36`: один пропуск даёт предупреждение, а не тревогу.
Одновременные запуски исключены `flock`.

Маркеры удачного прогона: `backup_run_started` → `backup_run_created` →
`backup_run_local_verified` → `backup_run_offhost_verified` либо
`backup_run_offhost_skipped` → `backup_run_retention_finished` → `backup_run_finished`.

Полный контракт — [backup-and-restore.md](backup-and-restore.md).

### 9.4 Временный режим local-only

Пока отдельного хранилища нет, допустим **явный** деградированный режим:

```env
FINSPACE_BACKUP_OFFHOST_ENABLED=false
```

Тогда dump, проверка восстановлением и backup set выполняются как обычно, внешняя копия
пропускается явно, в journald появляется `backup_run_degraded offhost_disabled=true`,
события `backup.remote.copy` нет, а `/api/v1/automation/backup/status` отвечает
`unverified`. **Это ожидаемое и правильное состояние, а не сбой.**

> [!WARNING]
> Проверенная локальная копия — **не** disaster recovery. Пока копия существует только на
> этом сервере, потеря сервера означает потерю данных. Статус остаётся `unverified`, пока
> не подтверждён второй домен отказа.

Когда отдельный хост появится, значения задаются в `/etc/finspace/backup.env` в общем
виде — конкретные адреса выбирает оператор, документация их не назначает:

```env
FINSPACE_BACKUP_OFFHOST_ENABLED=true
FINSPACE_BACKUP_REMOTE_HOST=<hostname приёмника>
FINSPACE_BACKUP_REMOTE_USER=<отдельная учётная запись на приёмнике>
FINSPACE_BACKUP_REMOTE_ROOT=<каталог назначения на приёмнике>
FINSPACE_BACKUP_SSH_KEY=/etc/finspace/id_backup       # root:root, 0600
FINSPACE_BACKUP_KNOWN_HOSTS=/etc/finspace/known_hosts # host key закреплён заранее
FINSPACE_BACKUP_REMOTE_LABEL=<непрозрачная метка для UI>
```

Приватный ключ создаётся на сервере и остаётся на нём: в Git он не попадает никогда.
Порядок переноса, проверка SHA-256 на приёмнике и поведение при отказе описаны в
[backup-and-restore.md](backup-and-restore.md#внешняя-копия-на-другой-хост).

## 9.5 Уборка staging импорта — необязательно

`./data/imports` хранит загруженные файлы импорта. Штатно они удаляются сразу при commit,
cancel или ошибке загрузки, поэтому каталог не растёт; остатки появляются только после
аварийной остановки процесса. Осмотр безопасен и ничего не удаляет:

```bash
cd /opt/finspace
sudo finspace-compose run --rm --no-deps backend python scripts/import_staging_reclaim.py
```

Периодическую уборку можно включить отдельным таймером — это необязательная часть
установки:

```bash
sudo install -m 0644 infrastructure/systemd/finspace-import-reclaim.service /etc/systemd/system/
sudo install -m 0644 infrastructure/systemd/finspace-import-reclaim.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finspace-import-reclaim.timer
```

Классы, grace-период и настройки: [import.md](import.md#освобождение-staging-reclamation).
Staging-файлы резервной копией не являются — за восстановление отвечает раздел 9.

## 10. n8n — опционально

n8n не нужен для работы финансового ядра и не участвует в восстановлении данных.
Устанавливайте его, только если нужны Telegram-бот, напоминания и отчёты.

- версия образа закреплена в Compose;
- состояние живёт в named volume `finspace_n8n_data`;
- `N8N_ENCRYPTION_KEY` обязателен для расшифровки credentials — без него сохранённые
  credentials восстановить нельзя, workflow при этом лежат в Git;
- доступ только `127.0.0.1:5678`; публиковать через tunnel/Funnel нельзя. Для
  администрирования используйте SSH port forwarding;
- обычный deploy приложения не должен останавливать и пересоздавать n8n.

Пошаговая настройка: [n8n.md](n8n.md), [telegram.md](telegram.md),
[automation-security.md](automation-security.md).

## 11. Внешний доступ: reverse proxy и Tailscale

Контейнеры Finspace слушают только loopback. Всё, что делает сервис доступным снаружи —
reverse proxy, Tailscale Serve/Funnel, DNS, сертификаты — это **внешняя инфраструктура,
не входящая в эту установку**.

Границы, которые нужно сохранить:

- восстановление данных не должно зависеть от конфигурации proxy или Tailscale: их
  восстановление — отдельная и необязательная задача;
- PostgreSQL, Redis, Adminer и n8n наружу не публикуются никогда;
- публичным делается только то, что действительно требуется интеграции (в текущем
  контуре — backend для Apps Script Bridge).

## 12. Контрольный список установки

Каждый пункт проверяется командой, а не впечатлением.

```bash
cd /opt/finspace
git rev-parse HEAD                       # ожидаемый релиз
git describe --exact-match --tags HEAD   # ожидаемый тег
./scripts/git-status-strict.sh           # чистое дерево, без диагностики Git

sudo finspace-compose config --quiet
sudo finspace-compose config --format json |
  python3 backend/scripts/validate_compose_topology.py production --stdin

sudo finspace-compose ps                 # postgres, redis, backend, frontend healthy
                                         # sync-worker, categorization-prune running
curl -fsS http://127.0.0.1:8000/api/v1/health
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
curl -fsS -o /dev/null -w 'login: HTTP %{http_code}\n' http://127.0.0.1:3000/login

sudo finspace-compose run --rm --no-deps backend alembic current   # head этого релиза

systemctl is-enabled finspace-backup.timer
systemctl is-active finspace-backup.timer
sudo systemctl start finspace-backup.service
journalctl -u finspace-backup.service -n 200 --no-pager
```

Ожидаемый результат:

- checkout — точный тег, дерево чистое;
- `finspace-compose config` завершается успешно, validator печатает
  `production topology: PASS`;
- postgres, redis, backend, frontend — healthy; sync-worker и categorization-prune —
  `Up` (у них нет healthcheck: доказательством работы служат циклы в логах);
- n8n healthy, если он установлен;
- `alembic current` равен head выбранного релиза;
- таймер `enabled` и `active`;
- ручной прогон backup завершается `backup_run_finished`, появляется один новый dump,
  его backup set имеет `local_verified=true`.

При `FINSPACE_BACKUP_OFFHOST_ENABLED=false` статус backup **штатно** равен `unverified`,
а `offhost_verified` равен `false`. Это ожидаемый результат установки, но это **не**
приёмка disaster recovery для v1.0: она закрывается только реальным вторым доменом отказа
и реальным переносом на него.

## 13. Чего не делать

- `sudo -E` — не работает и создаёт ложное ощущение переданного окружения;
- зависимость любой процедуры от `/etc/finspace/compose.server.yml`;
- `docker compose` без production-оверлея на боевом сервере;
- `chmod -R 777`, рекурсивный `chown -R root`, `git config --global safe.directory=*`;
- `git reset --hard`, `git clean -fdx`, `docker compose down -v`, удаление volume ради
  «починки» приложения;
- запуск миграций из одного релиза для приложения другого релиза;
- хранение `.env`, приватных ключей или бэкапов в Git;
- публикация PostgreSQL, Redis, Adminer или n8n наружу.

## 14. Дальше

- обновление и откат: [upgrade.md](upgrade.md);
- повседневная эксплуатация: [operations-runbook.md](operations-runbook.md);
- backup, restore и внешняя копия: [backup-and-restore.md](backup-and-restore.md);
- модель безопасности: [security.md](security.md);
- production-топология frontend: [frontend-production.md](frontend-production.md).
