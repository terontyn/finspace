# Локальный n8n

n8n используется только как планировщик и транспорт. Финансовые правила, проверки,
транзакции, аудит и `sync_outbox` остаются в Backend. Контейнер закреплён на версии
`2.30.5`, доступен только на `127.0.0.1:5678`, хранит состояние в named volume
`finspace_n8n_data` и подключён только к сети `finspace_automation_network` вместе с
Backend. PostgreSQL и Redis к этой сети не подключены.

## Первый запуск

1. Создайте отдельный случайный ключ и сохраните его только в локальном `.env`:

   ```powershell
   $bytes = New-Object byte[] 32
   $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
   try {
     $rng.GetBytes($bytes)
     [Convert]::ToBase64String($bytes)
   } finally {
     $rng.Dispose()
   }
   ```

   Замените `N8N_ENCRYPTION_KEY` полученным значением. Пустой или примерный ключ
   блокирует запуск n8n, но не мешает запуску остальных Compose-сервисов.

2. Запустите сервис и откройте локальную owner-настройку:

   ```powershell
   make n8n-up
   make n8n-status
   ```

   Адрес: <http://localhost:5678>. Не публикуйте этот порт через tunnel.

3. В разделе приложения **Автоматизации** создайте workspace-scoped service account
   типа `n8n` с минимальными нужными permissions. Одноразовый ключ сразу сохраните в
   n8n как credential типа Header Auth: имя `Authorization`, значение
   `ServiceKey <secret>`. Повторно Backend secret не показывает.

4. Создайте Telegram API credential в n8n. Bot token должен находиться только в этом
   зашифрованном credential store.

5. Импортируйте workflow, назначьте созданные credentials HTTP/Telegram-узлам,
   проверьте каждый workflow вручную и только затем активируйте:

   ```powershell
   make n8n-import
   ```

## Workflows

- `01-recurring-rules.json` — получает due rules и передаёт Backend только `rule_id` и
  `scheduled_for`;
- `02-telegram-bot.json` — long polling, сообщения/callback в Backend и доставка ответа;
- `03-weekly-report.json` — недельный отчёт;
- `04-uncategorized-reminder.json` — ежедневное напоминание без пустых сообщений;
- `05-month-close-reminder.json` — prepare прошлого месяца без confirm;
- `06-backup-health.json` — уведомление только при stale/failed/missing backup.

URL Backend берётся из `FINSPACE_BACKEND_URL`. Service key и Telegram bot token не
входят в экспорт. Каждый изменяющий запрос использует `X-Idempotency-Key`.

## Экспорт, диагностика и хранение

```powershell
make n8n-export
make n8n-status
docker compose logs --tail 100 n8n
```

Перед добавлением экспортов в Git проверьте diff и contract tests. Каталог
`n8n/workflows/exported` предназначен для ревью, не для credential backup.

Compose отключает diagnostics/templates/version notifications, включает pruning и
исключает Execute Command, filesystem, PostgreSQL и Redis nodes. Успешные и ошибочные
executions хранятся ограниченное время согласно `N8N_EXECUTIONS_DATA_MAX_AGE_HOURS`.

## Backup n8n

PostgreSQL dump приложения не содержит n8n volume. Для аварийного восстановления
остановите n8n и сделайте отдельную защищённую копию Docker volume вместе с внешне
сохранённым `N8N_ENCRYPTION_KEY`. Без исходного ключа credentials из volume не
восстановить. Не помещайте ключ или расшифрованные credentials в backup приложения.
