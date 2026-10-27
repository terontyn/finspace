# Синхронизация PostgreSQL ↔ Google Sheets

## Основной provider: Apps Script Bridge

Финансовый service сохраняет entity, audit и `sync_outbox` одной DB-транзакцией. Apps
Script периодически вызывает `pull`; backend выбирает pending/retry или просроченные
processing events под row lock, увеличивает attempt и ставит lease. Ответ содержит
canonical normalized row. Event остаётся processing до отдельного ACK.

Apps Script находит строку по техническому `_id`, применяет canonical row и отправляет
`applied` ACK с row number/hash. Ошибка возвращает event в retry с bounded backoff; после
окончания lease неподтверждённое событие снова выдаётся. Повторный ACK идемпотентен.
Initial export — обычные `full_export` events и становится completed только после ACK всех
строк.

Редактирование в Sheets не делает сеть внутри onEdit. Installable trigger сохраняет
устойчивый event ID в Document Properties queue. Scheduled `push` отправляет до 100
изменений. Backend проверяет HMAC/replay/workspace/spreadsheet, использует inbox
idempotency, optimistic locking, normalization, conflicts и audit. Новые строки разрешены
для операций; счета и категории редактируются только с существующим `_id`.

Reconciliation принимает компактный snapshot пакетами, сравнивает UUID/version/hash и
создаёт conflicts либо новые outbox events для восстановления. Удаление/отсутствие строки
в Sheets никогда не удаляет PostgreSQL entity.

Режимы binding: `bidirectional`, `paused`; во время initial export status — `initializing`.
Heartbeat, last pull и last ACK видны через status API.

## Необязательный provider: Google OAuth

Старый `google_oauth` использует Google API worker и сохранён для совместимости. Worker
явно выбирает только этот provider и не забирает Bridge events. OAuth включается отдельно
через `GOOGLE_OAUTH_ENABLED=true`; по умолчанию он выключен.
