# Finspace: эксплуатационная инструкция

Инструкция рассчитана на владельца self-hosted Finspace и следующего разработчика. Она
не содержит секретных значений. Подробные технические контракты остаются в тематических
документах; здесь собраны повседневные действия и безопасные команды.

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
| Production Compose wrapper | `sudo finspace-compose` |
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
```

Restart выполняйте только для нужного сервиса:

```bash
sudo finspace-compose restart backend
sudo finspace-compose restart frontend
```

После backend restart проверьте health, readiness и Google heartbeat. После frontend
restart проверьте `/login` и восстановление сессии.

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

Commit/push и deploy выполняются только после разрешения владельца. `release_ref` должен
быть точным проверенным tag или commit, а не плавающей веткой. Безопасная
последовательность обязательна:

1. Проверить clean worktree, текущую ревизию, backup policy и создать проверяемую точку
   восстановления.
2. Остановить только application services; PostgreSQL и Redis оставить работающими.
3. Получить и выбрать точный release.
4. Идемпотентно подготовить утверждённые writable runtime directories.
5. Установить version-controlled production override и проверить merged Compose.
6. Собрать images target release.
7. Выполнить migration из нового backend image.
8. Запустить backend.
9. Дождаться readiness.
10. Запустить sync-worker и frontend.
11. Выполнить smoke tests и проверить логи.

```bash
ssh finspace
cd /opt/finspace

./scripts/git-status-strict.sh
git rev-parse HEAD
sudo finspace-compose --profile tools run --rm backup \
  sh /scripts/verify-backup.sh --create

sudo finspace-compose stop frontend sync-worker backend

git fetch --tags origin
release_ref="EXACT_TAG_OR_COMMIT"
git switch --detach "$release_ref"
git rev-parse HEAD
sudo ./scripts/prepare-runtime-storage.sh /opt/finspace
./scripts/git-status-strict.sh

sudo cp -a /etc/finspace/compose.server.yml \
  "/etc/finspace/compose.server.yml.backup-$(date +%Y%m%d-%H%M%S)"
sudo install -o root -g root -m 0644 \
  /opt/finspace/compose.production.yml /etc/finspace/compose.server.yml
sudo finspace-compose config --quiet
sudo finspace-compose config --format json |
  python3 backend/scripts/validate_compose_topology.py production --stdin

sudo finspace-compose build backend sync-worker frontend
sudo finspace-compose run --rm --no-deps backend alembic upgrade head

sudo finspace-compose up -d --no-deps --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/v1/health/ready

sudo finspace-compose up -d --no-deps --force-recreate sync-worker frontend
sudo finspace-compose ps
curl -fsS -o /dev/null -w 'login: HTTP %{http_code}\n' \
  http://127.0.0.1:3000/login
docker exec finspace-backend-1 python scripts/google_config_check.py
```

Нельзя выполнять `git pull` поверх работающего source-mounted backend: изменение checkout
немедленно меняет исполняемый код и обходит build/restart boundary. В корректной
production-топологии source mounts отсутствуют, но application services всё равно
останавливаются до смены release, чтобы порядок deploy оставался однозначным. Никогда не
делайте автоматический downgrade production DB.

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
