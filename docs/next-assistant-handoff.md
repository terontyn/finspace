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

Локальная контрольная точка на 2026-08-23:
- ветка: codex/finspace-ui-integration;
- функциональная commit series: 2b666cd, ec2e737, 743e171, 8e3cad0;
- локальные commits не отправлены и не развёрнуты;
- последний подтверждённый production commit: ec4abe7; не считай его текущим без
  повторной read-only проверки сервера;
- baseline этапов 1–6: tag local-v0.6.0, commit cfc8276;
- новый routed App Shell, Dashboard, Accounts, Categories, Transactions, Reports,
  account reconciliation, hardened import и durable Apps Script queue реализованы и
  прошли локальные проверки;
- frontend: 72 tests; backend: 67 passed, 1 skipped; Apps Script harness: 10 tests;
- отдельная живая E2E-книга 2026-08-23 доказала безопасный retry после DNS и HTTP 503 и
  затем была полностью удалена вместе с binding/triggers/тестовыми объектами;
- frontend: Next.js production, next start, healthy, 127.0.0.1:3000;
- backend: FastAPI, healthy, 127.0.0.1:8000;
- PostgreSQL и Redis healthy;
- sync-worker запущен;
- frontend доступен внутри tailnet: https://terontyn-pc.tailfcdf00.ts.net/;
- Apps Script backend опубликован Tailscale Funnel:
  https://terontyn-pc.tailfcdf00.ts.net:8443;
- браузер использует same-origin /api/* через Next.js, а не прямой backend 8443;
- cloudflared inactive/disabled; api.terontyn.site оставлен только как резервная
  Cloudflare-конфигурация и сейчас не является рабочим backend URL;
- основной Google provider: apps_script_bridge;
- Google-книга зарегистрирована, triggers каждые 5 минут, heartbeat активен,
  двусторонняя синхронизация подтверждена;
- n8n и Adminer на production-сервере не запущены;
- n8n/Telegram/automation код реализован, но пользовательская production-активация не
  завершена;
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
5. Production frontend использует compose.production.yml/server override, next start,
   same-origin API и непривилегированного пользователя.
6. Не запускай next dev на production.
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
mypy и релевантные targeted tests. Для Docker/production изменений дополнительно проверь
production image/Compose в объёме риска.

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
