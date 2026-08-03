# Безопасность автоматизаций

## Граница доверия

Разрешённый поток: `n8n → Backend Automation API → domain service → PostgreSQL + audit +
sync_outbox`. n8n не подключён к сети PostgreSQL/Redis, не получает их credentials и не
содержит финансовую бизнес-логику. Backend повторно валидирует workspace и все объекты.

## ServiceKey

Service account отделён от пользователя и не работает как JWT. Owner задаёт тип,
workspace scope и allow-list permissions. Secret возвращается один раз; в БД остаются
только безопасный prefix и SHA-256 hash. Проверяются status, expiry, revoke, permission и
workspace. Rotation атомарно отзывает старые ключи. Запрещённые возможности вроде
`users:manage`, `workspace:delete`, `audit:delete`, `backup:restore` и произвольной записи
транзакций не выдаются по умолчанию.

Минимальный n8n account обычно получает:

```text
automation:read
automation:execute
recurring:read
recurring:execute
reports:generate
notifications:send
month-close:prepare
backup:status
```

Все automation calls передают `X-Idempotency-Key`. `automation_runs` хранит только
минимальные input/result summaries, статус, request ID и ошибку без tokens, полного
Telegram update и полного финансового отчёта.

## Разделение секретов

- `N8N_ENCRYPTION_KEY` — только локальный `.env` и внешний защищённый recovery storage;
- ServiceKey — только n8n Header Auth credential;
- bot token — только n8n Telegram credential;
- user JWT, Google binding secret, Google encryption key, DB/Redis credentials — никогда
  не передаются n8n;
- workflow exports, audit, логи и документация не содержат plaintext secrets.

Компрометация ServiceKey требует немедленного revoke/rotate в разделе
**Автоматизации**, остановки workflow и проверки `automation_runs`/audit. Компрометация
bot token требует rotation через BotFather и обновления только n8n credential.

