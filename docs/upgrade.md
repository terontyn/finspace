# Обновление и откат Finspace

Документ описывает переход с релиза A на релиз B на уже установленном сервере и три
разных сценария отката. Первичная установка — [production-install.md](production-install.md).

Все команды предполагают установленный wrapper `finspace-compose` и защищённый `.env`,
поэтому выполняются через `sudo`. `sudo -E` на production не поддерживается; если команде
действительно нужна переменная, передавайте её как `sudo env VAR=value ...`.

## 1. Идентичность релиза

Production обновляется на **точный аннотированный тег**, а не на плавающую ветку: только
так «что сейчас работает» и «что мы восстанавливаем» — один и тот же объект. Уже
существующие теги неизменны; обновление никогда не двигает тег.

```bash
cd /opt/finspace
git rev-parse HEAD                       # текущий commit
git describe --exact-match --tags HEAD   # текущий тег
```

Работа с `main` допустима только в предрелизной разработке и только осознанно. В этом
случае «релиз» — точный commit, и он фиксируется в отчёте так же, как фиксировался бы тег.

## 2. PRECHECK

Ничего не меняем; собираем факты и создаём точку восстановления.

```bash
cd /opt/finspace

# 2.1 Что работает сейчас.
git rev-parse HEAD
git describe --exact-match --tags HEAD || echo "не тег: плавающий checkout"
./scripts/git-status-strict.sh

# 2.2 Сервисы здоровы до обновления, а не только после.
sudo finspace-compose ps
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
curl -fsS -o /dev/null -w 'login: HTTP %{http_code}\n' http://127.0.0.1:3000/login

# 2.3 Текущая ревизия схемы.
sudo finspace-compose run --rm --no-deps backend alembic current

# 2.4 Планировщик backup.
systemctl is-enabled finspace-backup.timer
systemctl is-active finspace-backup.timer

# 2.5 Место на диске.
df -h /opt/finspace /var/lib/docker
```

### 2.6 Свежий проверенный backup — до обновления

Это не формальность: это единственный откат, который работает во всех случаях
(см. [раздел 7](#7-таксономия-отката), CASE C).

```bash
sudo systemctl start finspace-backup.service
journalctl -u finspace-backup.service -n 200 --no-pager
```

Прогон обязан дойти до `backup_run_finished`, а backup set — иметь `local_verified=true`.
**Обновление не начинается, пока свежая копия не проверена восстановлением.** Совпадения
SHA-256 недостаточно: проверка поднимает временную БД, сверяет Alembic revision,
обязательные таблицы и колонки.

Запишите `set_id`, `alembic_revision` и commit этой копии: именно они понадобятся, если
дело дойдёт до восстановления.

## 3. FETCH

```bash
cd /opt/finspace
git fetch --tags origin
release_ref="EXACT_TAG"                 # целевой релиз
git rev-parse "$release_ref^{commit}"   # ожидаемый commit цели
```

Сверьте полученный commit с тем, который вы намеревались устанавливать. Checkout ещё не
меняется.

## 4. Совместимость схемы

Прежде чем что-либо переключать, ответьте на три вопроса.

1. **Какая ревизия сейчас в базе** — вывод `alembic current` из PRECHECK.
2. **Какой head у целевого релиза** — список миграций цели без переключения checkout:

   ```bash
   git ls-tree --name-only "$release_ref" backend/alembic/versions/
   ```

3. **Направление**: текущая ревизия базы обязана присутствовать среди миграций целевого
   релиза. Если файла текущей ревизии в целевом релизе нет — цель **старше** базы, и это
   не обновление, а откат приложения на несовместимую схему.

> [!CAUTION]
> **Никогда не запускайте приложение старого релиза на базе, уже мигрированной дальше,
> чем этот релиз поддерживает.**
> Запрещённая последовательность: мигрировать базу на релиз B → переключиться на релиз A →
> запустить A. Это разрешено только если релиз B в своих release notes явно объявляет
> обратную совместимость схемы. По умолчанию такого объявления нет.
>
> До 1.0 политика Finspace: **миграции операционно однонаправленные**. `alembic downgrade`
> не является штатным механизмом отката production и не используется как способ «вернуть
> как было».

### 4.1 Migration gate релиза

Целостность самой цепочки миграций проверяется отдельным воспроизводимым гейтом. Он
запускается **до** релиза, на изолированной временной базе, и не имеет отношения к
production-базе:

```bash
cd /opt/finspace
sudo finspace-compose run --rm --no-deps -e TESTING=true backend \
  python scripts/validate_migrations.py \
  --expect-head 0017_categorization_history --expect-count 17
```

Что он доказывает:

- ровно одна голова и ровно один корень, связный линейный граф без циклов, дублей
  revision id, висящих `down_revision`, branch labels и merge-ревизий;
- чистая пустая PostgreSQL доходит до head;
- повторный `alembic upgrade head` ничего не меняет;
- набор таблиц после миграций точно совпадает с метаданными SQLAlchemy;
- база, поднятая до исторической контрольной точки (`0001`, `0005`, `0010`, `0016`),
  успешно мигрирует вперёд до head.

Только граф, без базы: `--static-only`.

> [!IMPORTANT]
> Гейт проверяет **цепочку релиза**, а не ваш сервер. Он не заменяет порядок обновления
> из этого документа: PRECHECK со свежей проверенной копией → точный релиз → проверка
> совместимости → `alembic upgrade head` → deploy → POSTCHECK. Гейт создаёт и удаляет
> только собственные временные базы вида `finspace_test_<uuid>`; production-базы он не
> касается, а `alembic downgrade` не выполняет никогда.

Правила совместимости при восстановлении из резервной копии не меняются:
[раздел 8](#8-совместимость-restore-и-версии-схемы).

## 5. DEPLOY

```bash
cd /opt/finspace

# 5.1 Точный релиз.
git switch --detach "$release_ref"
git rev-parse HEAD
./scripts/git-status-strict.sh

# 5.2 Wrapper и runtime-каталоги приводятся к контракту этого релиза.
sudo ./scripts/install-finspace-compose.sh
sudo ./scripts/prepare-runtime-storage.sh /opt/finspace

# 5.2.1 Существующие хосты: не осталась ли в backup.env ссылка на прежний wrapper.
sudo ./scripts/check-backup-env-wrapper.sh

# 5.3 Топология — до сборки.
sudo finspace-compose config --quiet
sudo finspace-compose config --format json |
  python3 backend/scripts/validate_compose_topology.py production --stdin

# 5.4 Образы изменившихся сервисов.
sudo finspace-compose build backend sync-worker categorization-prune frontend

# 5.5 Миграции — из образа целевого релиза, приложение ещё не перезапущено.
sudo finspace-compose run --rm --no-deps backend alembic upgrade head
sudo finspace-compose run --rm --no-deps backend alembic current

# 5.6 Пересоздаются только нужные сервисы.
sudo finspace-compose up -d --no-deps --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
sudo finspace-compose up -d --no-deps --force-recreate sync-worker categorization-prune frontend
sudo finspace-compose ps
```

Что здесь важно:

- миграции выполняются образом **целевого** релиза, а не текущего;
- `--no-deps` не даёт Compose заодно пересоздать PostgreSQL и Redis;
- n8n не перезапускается, если релиз не содержит изменений n8n;
- собирать нужно только те сервисы, чьи образы изменились; если изменился только
  frontend, соберите и пересоздайте только его.

**Frontend в production не имеет bind mount исходников.** Изменение checkout само по себе
не меняет работающий код: нужны `build` и `up -d --force-recreate`. Просто `restart` не
подхватит новую версию. То же верно для backend и обоих worker-процессов.

### 5.2.1 Существующие хосты: ссылка на прежний wrapper в `backup.env`

Хосты, установленные до того, как wrapper стал файлом из репозитория, могут хранить в
`/etc/finspace/backup.env` абсолютный путь к прежнему расположению:

```env
FINSPACE_COMPOSE=/usr/local/sbin/finspace-compose
```

Установка нового wrapper эту строку **не исправляет**. После ретирования прежнего файла
`finspace-backup.service` начинает падать, а остальное выглядит здоровым: ошибка видна только
в journal, и только если туда посмотреть. Это уже происходило на production.

`install-finspace-compose.sh` вызывает `check-backup-env-wrapper.sh` сам и завершается кодом
**3**, если нужна починка, поэтому deploy под `set -e` останавливается здесь, а не объявляет
обновление завершённым. Проверку можно запустить и отдельно — она только читает файл и
никогда его не переписывает:

```bash
sudo ./scripts/check-backup-env-wrapper.sh
```

Починка — одна строка, её вносит оператор:

```env
FINSPACE_COMPOSE=/usr/local/bin/finspace-compose
```

Допустимо и голое имя `finspace-compose`: оно разрешается через `PATH` и потому не устаревает
при переносе. Абсолютный путь фиксирует ровно одно место и именно поэтому ломается.

После правки обновление считается завершённым только после **успешного прогона backup**:

```bash
sudo systemctl daemon-reload
sudo systemctl start finspace-backup.service
systemctl is-enabled finspace-backup.timer
systemctl is-active  finspace-backup.timer
journalctl -u finspace-backup.service -n 100 --no-pager
```

Ожидаются маркеры `backup_run_started` → `backup_run_created` → `backup_run_local_verified` →
`backup_run_finished`. Отредактированный `backup.env` без успешного прогона ничего не
доказывает.

## 6. POSTCHECK

```bash
cd /opt/finspace
git rev-parse HEAD
git describe --exact-match --tags HEAD
./scripts/git-status-strict.sh

sudo finspace-compose ps
curl -fsS http://127.0.0.1:8000/api/v1/health
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
curl -fsS -o /dev/null -w 'login: HTTP %{http_code}\n' http://127.0.0.1:3000/login
sudo finspace-compose run --rm --no-deps backend alembic current

sudo finspace-compose logs --tail 100 sync-worker
sudo finspace-compose logs --tail 100 categorization-prune

systemctl is-enabled finspace-backup.timer
systemctl is-active finspace-backup.timer
```

Контрольный список обновления:

- свежая копия до обновления создана и проверена восстановлением;
- checkout — ровно целевой тег/commit, дерево чистое;
- `finspace-compose config` успешен, validator даёт `production topology: PASS`;
- миграция выполнена образом целевого релиза, `alembic current` равен его head;
- пересозданы только запланированные контейнеры; PostgreSQL, Redis и n8n не тронуты;
- backend и frontend healthy, оба worker-процесса пишут свои циклы в логи;
- n8n healthy, если он используется;
- `check-backup-env-wrapper.sh` завершается успешно;
- таймер backup остался `enabled` и `active`, и после ретирования прежнего wrapper выполнен
  **успешный** прогон `finspace-backup.service`.

## 7. Таксономия отката

Откат выбирается не по желанию, а по тому, **успела ли отработать миграция**.

### CASE A — миграции ещё не выполнялись

Сборка или запуск целевого релиза упали до шага 5.5. Схема не менялась, база
соответствует прежнему релизу.

```bash
cd /opt/finspace
git switch --detach "$previous_release_ref"
sudo ./scripts/install-finspace-compose.sh
sudo finspace-compose config --quiet
sudo finspace-compose config --format json |
  python3 backend/scripts/validate_compose_topology.py production --stdin
sudo finspace-compose build backend sync-worker categorization-prune frontend
sudo finspace-compose up -d --no-deps --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
sudo finspace-compose up -d --no-deps --force-recreate sync-worker categorization-prune frontend
sudo finspace-compose run --rm --no-deps backend alembic current
```

Восстановление данных не требуется. `alembic current` обязан показать ту же ревизию, что
и в PRECHECK.

### CASE B — миграции выполнены и явно объявлены обратно совместимыми

Допустим **только** если release notes целевого релиза прямо утверждают, что предыдущий
релиз работает на новой схеме. Тогда достаточно отката приложения — по процедуре CASE A,
без изменения схемы.

**По умолчанию считайте, что это не ваш случай.** Отсутствие явного утверждения — это
CASE C, а не CASE B.

### CASE C — миграции выполнены, совместимость неизвестна или небезопасна

Старое приложение на новой схеме не запускается. Откат — восстановление из копии,
сделанной в PRECHECK, а не разрушающий downgrade схемы.

```bash
cd /opt/finspace

# 1. Остановить запись: приложение, worker-процессы и, при необходимости, n8n.
sudo finspace-compose stop frontend backend sync-worker categorization-prune

# 2. Вернуть точный предыдущий релиз.
git switch --detach "$previous_release_ref"
git rev-parse HEAD
sudo ./scripts/install-finspace-compose.sh

# 3. Восстановить PostgreSQL из проверенной копии, сделанной ДО обновления.
#    Порядок, подтверждения и требования к перезаписи рабочей БД — backup-and-restore.md.

# 4. Поднять предыдущий релиз и убедиться, что схема соответствует ему.
sudo finspace-compose build backend sync-worker categorization-prune frontend
sudo finspace-compose up -d --no-deps --force-recreate backend
sudo finspace-compose run --rm --no-deps backend alembic current
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
sudo finspace-compose up -d --no-deps --force-recreate sync-worker categorization-prune frontend
```

Порядок восстановления, подтверждения перезаписи рабочей БД и точный контракт
`restore.sh` — [backup-and-restore.md](backup-and-restore.md#ручное-безопасное-восстановление).
Перезапись рабочей БД требует одновременно точного пути, `--overwrite-main` и явной
подтверждающей строки; выполняйте её только при остановленных backend и frontend.

**Цена CASE C — данные, записанные после создания копии.** Это ещё одна причина, по
которой backup из PRECHECK создаётся непосредственно перед обновлением, а не «вчера».

### Когда откат небезопасен: эскалация

Если после миграции уже прошли пользовательские записи, а восстановление из копии
означает потерю значимых данных, откат перестаёт быть технической операцией.

1. Остановите запись: `sudo finspace-compose stop frontend backend sync-worker categorization-prune`.
   PostgreSQL и Redis не трогайте.
2. Сделайте **ещё одну** копию — текущего, уже мигрированного состояния. Она понадобится
   независимо от выбранного решения и не должна перезаписывать копию из PRECHECK.
3. Зафиксируйте факты: commit до и после, `alembic current` до и после, точное время
   миграции, последние `backup_run_*` в journald, симптом.
4. Решение принимает владелец: чинить вперёд на новом релизе или принять потерю записей
   после точки восстановления. Не выбирайте за него и не делайте
   `alembic downgrade`, `docker compose down -v`, `git reset --hard` «пока никто не
   видит».

## 8. Совместимость restore и версии схемы

Каждый backup set несёт `alembic_revision`, `finspace_commit` и, если он был,
`finspace_tag`. Это и есть ответ на вопрос «каким релизом это можно поднимать».

| соотношение | что делать |
|---|---|
| revision копии = head выбранного релиза | восстанавливать и запускать |
| revision копии **новее**, чем поддерживает выбранный релиз | **не запускать этот релиз на этой копии**; выбрать релиз, который содержит эту ревизию |
| revision копии старее целевого релиза | восстановить, затем `alembic upgrade head` целевого релиза |

Порядок в последнем случае строго такой: сначала restore, потом миграция вперёд образом
целевого релиза. Обратный порядок или «подгонка» схемы вручную запрещены.

**Восстановленную базу никогда не понижают по схеме молча.** Если нужный релиз неизвестен,
он определяется по `alembic_revision` из backup set, а не подбором.

Восстановленный дамп содержит всю финансовую историю. Google-токены лежат в нём только как
ciphertext: без внешне сохранённого `GOOGLE_TOKEN_ENCRYPTION_KEY` и его версии
Google-подключения придётся создавать заново. Это не потеря финансовых данных.

## 9. Подготовленные приёмочные сценарии

Оба сценария подготовлены, но не выполняются в рамках изменения документации.

### FLOW 1 — репетиция чистой установки

На отдельной чистой машине (или в отдельном каталоге на тестовом хосте) пройти
[production-install.md](production-install.md) целиком. Цель — доказать отсутствие
зависимости от:

- `/etc/finspace/compose.server.yml`;
- shell-алиасов и прежнего варианта `finspace-compose` на действующем сервере;
- существующего `.env`;
- существующих Docker volume;
- любых ручных исправлений, которых нет в документации.

Успех: контрольный список раздела 12 установки проходит полностью, а
`FINSPACE_BACKUP_OFFHOST_ENABLED=false` даёт ожидаемый `unverified`.

### FLOW 2 — репетиция обновления

С предыдущего точного релиза на целевой: PRECHECK со свежей проверенной копией → FETCH →
проверка совместимости схемы → DEPLOY → POSTCHECK. Успех — весь контрольный список
раздела 6, включая «пересозданы только запланированные контейнеры».

Полная репетиция восстановления данных (disaster recovery drill) в эти два сценария не
входит и выполняется отдельно.
