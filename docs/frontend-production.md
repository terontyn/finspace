# Production deployment and runtime topology

Базовый `docker-compose.yml` остаётся конфигурацией разработки: он собирает target
`development`, монтирует исходники, `node_modules` и `.next`, затем запускает `next dev`.
Этот режим нельзя использовать за HTTPS reverse proxy: HMR-клиент и dev-ресурсы зависят
от origin/WebSocket-подключения, а браузер может заблокировать `/_next/webpack-hmr`. Если
клиентские chunks не загружены или HMR bootstrap заблокирован, остаётся только серверный
HTML и React effects, включая восстановление сессии, не запускаются.

## Production image

`frontend/Dockerfile` содержит независимые stages:

- `development` — локальный `next dev`;
- `builder` — `npm ci` и `npm run build` с публичным build argument;
- `production-dependencies` — только runtime dependencies через `npm ci --omit=dev`;
- `production` — готовая `.next`, public assets и запуск `npm run start` от пользователя
  `app`.

Единственная переменная, которая встраивается в клиентский JavaScript, —
`NEXT_PUBLIC_API_URL`. В production она равна `/`: браузер вызывает same-origin
`/api/v1/...`, а Next.js route proxy обращается к backend только во внутренней Docker-сети.
Переменная не является секретом, но должна быть передана именно при build:

```bash
docker build \
  --build-arg NEXT_PUBLIC_API_URL=/ \
  --target production \
  -t finspace-frontend:production \
  ./frontend
```

`INTERNAL_API_URL` используется серверным proxy и по умолчанию равен
`http://backend:8000`. Это не публичная переменная и не build secret; production Compose
передаёт её только runtime-контейнеру. Proxy сохраняет HTTP method, query, body, cookies,
status, response body и end-to-end headers, включая `Set-Cookie`, но удаляет hop-by-hop
headers. JWT, пароли, Google, n8n и другие секреты нельзя передавать как build args или
в frontend environment.

Прямой `https://host:8443` не используется frontend: это устраняет зависимость auth от
браузерной политики доступа к нестандартному порту и делает refresh cookie first-party.

## Production Compose

Production запускается вместе с базовой конфигурацией и override:

```bash
docker compose -f docker-compose.yml -f compose.production.yml config --quiet
python3 backend/scripts/validate_compose_topology.py all
docker compose -f docker-compose.yml -f compose.production.yml build \
  backend sync-worker frontend
```

`docker-compose.yml` остаётся удобным development base:

- backend и sync-worker монтируют `./backend:/app`;
- backend запускает `uvicorn --reload`;
- frontend собирает target `development` и монтирует исходники, `node_modules`, `.next`.

`compose.production.yml` явно удаляет эти development defaults:

- backend получает production-команду без `--reload`, а `/app` остаётся кодом из image;
- sync-worker не имеет mounts и использует код из того же backend image;
- frontend собирает target `production`, запускает `npm run start` и не имеет mounts;
- backend сохраняет только утверждённые runtime bind mounts для imports, acceptance
  artifacts, backup metadata, Apps Script package и n8n operational package.

Для замены backend volume list используется Compose tag `!override`; для удаления всех
worker/frontend volumes — `!reset`. Эти теги предотвращают list merge с dev mounts. Host
bindings и networks наследуются из base Compose.

### Утверждённые backend mounts

| Source | Target | Режим | Назначение |
|---|---|---|---|
| `./data/imports` | `/app/data/imports` | read-write | Staging-файлы импорта |
| `./data/acceptance` | `/app/data/acceptance` | read-write | Ограниченный registry live acceptance |
| `./backups/acceptance-reports` | `/app/data/acceptance-reports` | read-write | Обезличенные acceptance reports |
| `./backups` | `/app/backups` | read-only | Статус и metadata проверенных backup |
| `./google-apps-script` | `/app/google-apps-script` | read-only | Выдаваемый backend Apps Script package |
| `./n8n` | `/app/n8n` | read-only | Version-controlled operational package |

Ни один из этих mounts не заменяет `/app` целиком. PostgreSQL/Redis data продолжают
храниться в именованных volumes своих сервисов.

Три read-write каталога создаются и приводятся к контракту backend image командой
`sudo ./scripts/prepare-runtime-storage.sh /opt/finspace`. Источник UID/GID —
`backend/runtime-identity.env`; текущий контракт `app` равен `100:101`, mode каталогов —
`0750`. Скрипт не меняет содержимое каталогов рекурсивно, `./backups` целиком,
PostgreSQL/Redis/n8n volumes или другие пути. Production sync-worker mounts не имеет.

### Детерминированная проверка

```bash
# Development и repository production merge:
python3 backend/scripts/validate_compose_topology.py all

# Уже собранный server wrapper без печати environment:
sudo finspace-compose config --format json |
  python3 backend/scripts/validate_compose_topology.py production --stdin
```

Проверка завершается ошибкой при source mount в production backend/worker/frontend,
`--reload`, неверной frontend-команде, лишнем mount или неверном read-only режиме.

## Server override

На Ubuntu содержимое `/etc/finspace/compose.server.yml` должно совпадать с
`compose.production.yml`. Если `finspace-compose` уже вызывает базовый файл и этот server
override, обновление выполняется только как часть безопасного release-порядка из
[operations-runbook.md](operations-runbook.md). Сам шаг установки выглядит так:

```bash
sudo cp -a /etc/finspace/compose.server.yml "/etc/finspace/compose.server.yml.backup-$(date +%Y%m%d-%H%M%S)"
sudo install -o root -g root -m 0644 /opt/finspace/compose.production.yml /etc/finspace/compose.server.yml
sudo finspace-compose config --quiet
sudo finspace-compose config --format json |
  python3 backend/scripts/validate_compose_topology.py production --stdin
```

Не копируйте override и не меняйте checkout в обход полного deploy-порядка, если
application services продолжают работать из source mounts. После запуска в логах frontend
должна присутствовать строка `next start`, а запросов к `/_next/webpack-hmr` быть не
должно. Проверка с хоста:

```bash
curl -fsS http://127.0.0.1:3000/login >/dev/null
curl -fsS https://terontyn-pc.tailfcdf00.ts.net/ >/dev/null
```
