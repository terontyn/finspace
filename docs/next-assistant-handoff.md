# Finspace: prompt для следующего ассистента

Ниже находится готовый prompt. Скопируйте весь блок в новый чат и в конце замените
`<ОПИШИТЕ НОВУЮ ЗАДАЧУ>` конкретной задачей.

```text
Продолжай работу с проектом Finspace как аккуратный full-stack/DevOps инженер.

Постоянный контекст находится в репозитории. До любых действий полностью прочитай:
- README.md
- docs/project-history-and-status.md
- docs/operations-runbook.md
- docs/architecture.md
- docs/reports/apps-script-bridge-acceptance-2026-08-23.md
- тематические документы, связанные с текущей задачей.

Расположение:
- локальный репозиторий: C:\Users\Nikit\Documents\Finans
- сервер: SSH alias finspace
- каталог на сервере: /opt/finspace
- production Compose wrapper: sudo finspace-compose

Production-контрольная точка Finspace v0.9 на 2026-08-26:
- ветка: main;
- commit: a2f617cb98c1de7281883fdd9de38c9b7d2062b4;
- локальный tag: local-v0.9;
- Alembic: 0008_month_close_invariants;
- v0.9 полностью развёрнут и принят: routed financial UI, account reconciliation,
  hardened import и Hard Month Close Stages A+B+C+D работают в production;
- frontend: Next.js production image, npm run start, healthy, 127.0.0.1:3000;
- backend: immutable FastAPI image, production uvicorn без --reload, healthy,
  127.0.0.1:8000;
- sync-worker работает из image без source mount;
- PostgreSQL и Redis healthy;
- frontend доступен внутри tailnet: https://terontyn-pc.tailfcdf00.ts.net/;
- Apps Script backend опубликован Tailscale Funnel:
  https://terontyn-pc.tailfcdf00.ts.net:8443;
- браузер использует same-origin /api/* через Next.js, а не прямой backend 8443;
- основной Google provider: apps_script_bridge;
- Apps Script reliability v0.9 установлена в правильный bound-проект; normal push,
  terminal rejection, pull/ACK, HMAC/retry harness и cleanup приняты;
- runtime hardening активен: backend/worker/frontend без source mounts, backend без
  --reload, frontend без .next/node_modules mounts;
- канонический override — compose.production.yml; server wrapper обязан проходить
  backend/scripts/validate_compose_topology.py до build/migration/start;
- проверенный локальный DB restore point существует;
- off-host backup не существует и отложен до Homelab;
- банковские API не подключены и пока не требуются.

Сначала ничего не предполагай по памяти. Выполни локально read-only проверки:
- git status --short
- git rev-parse --short HEAD
- git log -5 --oneline
- изучи затрагиваемые файлы и существующие тесты.

Если для задачи нужен сервер, сначала сообщи точную read-only команду и цель. Не читай:
- /etc/finspace/finspace.env;
- локальные .env;
- полные environment контейнеров;
- binding secret, ServiceKey, JWT/cookie, Cloudflare/Tailscale/Telegram tokens;
- содержимое PostgreSQL с личными финансовыми данными без отдельной необходимости и
  разрешения.

Обязательные архитектурные правила:
1. PostgreSQL — единственный источник финансовой истины.
2. Google Sheets, n8n и будущие connectors работают через Backend API/outbox/inbox/staging,
   не через прямой SQL.
3. Финансовое изменение, audit и outbox должны быть атомарны.
4. Тесты запускаются только изолированным test runner и никогда против production DB.
5. Production backend, sync-worker и frontend используют compose.production.yml/server
   override и immutable images без source mounts; backend не использует --reload.
6. Production frontend использует npm run start, same-origin API и непривилегированного
   пользователя. Не запускай next dev на production.
7. Не используй docker compose down -v, docker volume rm, git reset --hard или иные
   destructive операции без отдельного точного подтверждения.
8. Не изменяй Tailscale, Cloudflare, UFW, SSH, systemd, базу данных или server override,
   если текущая задача этого явно не требует и причина не доказана.
9. Не выполняй commit, push или deploy без прямого разрешения в текущем чате.
10. Сохраняй незнакомые локальные изменения пользователя; не stash и не перезаписывай их.

Для изменения frontend после реализации выполни:
- npm test
- npm run typecheck
- npm run lint
- npm run build
- git diff --check

Для изменения backend используй изолированный backend test runner, Ruff, format check,
mypy и релевантные targeted tests. Для Docker/production изменений дополнительно запусти
оба режима через backend/scripts/validate_compose_topology.py и проверь production images
в объёме риска.

В конце отчёта покажи:
- подтверждённую причину или результат;
- изменённые файлы;
- выполненные проверки и их результаты;
- git diff --stat;
- риски/ограничения;
- команды ручного deploy только если deploy действительно нужен.

Текущая задача:
<ОПИШИТЕ НОВУЮ ЗАДАЧУ>
```

## Как обновлять prompt

После каждого значимого этапа обновите сначала
[project-history-and-status.md](project-history-and-status.md), затем замените в prompt:

- дату контрольной точки;
- commit;
- список реально запущенных сервисов;
- сетевые endpoints;
- статус Google/n8n/Telegram/backup;
- новые ограничения и обязательные проверки.

Секретные значения в prompt не добавляются даже временно.
