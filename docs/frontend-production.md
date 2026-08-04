# Production deployment frontend

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
`NEXT_PUBLIC_API_URL`. Она не является секретом, но должна быть передана именно при build:

```bash
docker build \
  --build-arg NEXT_PUBLIC_API_URL=https://terontyn-pc.tailfcdf00.ts.net:8443 \
  --target production \
  -t finspace-frontend:production \
  ./frontend
```

`INTERNAL_API_URL` используется серверным rewrite. Это не публичная переменная и не build
secret; production Compose передаёт её только runtime-контейнеру. JWT, пароли, Google,
n8n и другие секреты нельзя передавать как build args или в frontend environment.

## Production Compose

Production запускается вместе с базовой конфигурацией и override:

```bash
docker compose -f docker-compose.yml -f compose.production.yml build frontend
docker compose -f docker-compose.yml -f compose.production.yml up -d --no-deps frontend
docker compose -f docker-compose.yml -f compose.production.yml ps frontend
docker compose -f docker-compose.yml -f compose.production.yml logs --tail 100 frontend
```

Override использует Compose tag `!reset`, поэтому у production frontend нет bind mount
исходников, `/app/node_modules`, `/app/.next` и общего `env_file`. На порту контейнера 3000
работает `next start`; host binding `127.0.0.1:3000` наследуется из базовой конфигурации.

## Server override

На Ubuntu содержимое `/etc/finspace/compose.server.yml` должно совпадать с
`compose.production.yml`. Если `finspace-compose` уже вызывает базовый файл и этот server
override, ручное обновление выполняется так:

```bash
sudo install -o root -g root -m 0644 /opt/finspace/compose.production.yml /etc/finspace/compose.server.yml
sudo finspace-compose build --no-cache frontend
sudo finspace-compose up -d --no-deps frontend
sudo finspace-compose ps frontend
sudo finspace-compose logs --tail 100 frontend
```

После запуска в логах frontend должна присутствовать строка `next start`, а запросов к
`/_next/webpack-hmr` быть не должно. Проверка с хоста:

```bash
curl -fsS http://127.0.0.1:3000/login >/dev/null
curl -fsS https://terontyn-pc.tailfcdf00.ts.net/ >/dev/null
```
