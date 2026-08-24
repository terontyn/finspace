# Сверка счёта с банковской выпиской

Account reconciliation — отдельный bounded context. Он не использует и не изменяет Google Sheets reconciliation (`app/services/reconciliation.py`).

## Модель

- `account_reconciliations` хранит только подтверждённые сверки: счёт и workspace, дату и валюту выписки, банковский и рассчитанный баланс, нулевую разницу, preview token, ожидаемую версию счёта, idempotency key, автора и время подтверждения.
- `account_reconciliation_items` фиксирует точный набор операций и их версии на момент подтверждения.
- Баланс в `accounts` не хранится и не изменяется: он остаётся производным от начального баланса и финансовых операций.

Preview эфемерен и не создаёт записи в БД. Подтверждённая сверка immutable; текущая версия не поддерживает отмену или переоткрытие сверки.

## Временная граница и расчёт

`statement_date` — календарная дата в timezone рабочего пространства. В расчёт входят операции до начала следующего локального дня (`occurred_at < cutoff_at`), но не раньше `opening_balance_at`.

Расчётный баланс равен opening balance плюс влияние не удалённых операций со статусом `confirmed` или `reconciled`:

- income и adjustment увеличивают баланс;
- expense уменьшает;
- transfer уменьшает исходный и увеличивает целевой счёт;
- refund обращает влияние связанной income/expense.

Draft, cancelled и deleted не влияют на баланс и не выбираются для подтверждения.

Кандидатами становятся операции, влияющие на счёт, которые ещё не входят в подтверждённую сверку этого счёта. Перевод связывается со сверкой отдельно на каждой стороне: сверка исходного счёта не исключает его из будущей сверки целевого.

## Политика переводов: global lock, per-account evidence

Для transfer используется минимальная политика **global lock**:

- глобальный `transaction.status = reconciled` означает, что весь логический перевод защищён от edit, cancel и delete;
- этот статус **не означает**, что влияние перевода уже сверено на обоих счетах;
- источником истины для конкретной стороны остаётся `account_reconciliation_items` вместе с `account_reconciliations.account_id`;
- исходный и целевой счета подтверждают перевод независимо, но одна и та же сторона не может стать кандидатом повторно;
- после сверки первой стороны вторая сторона продолжает видеть то же влияние перевода в рассчитанном балансе и получает transfer как кандидата;
- после сверки обеих сторон transfer больше не является кандидатом ни для одного счёта.

Global lock выбран потому, что изменение суммы, даты, исходного или целевого счёта после первой сверки сделало бы уже подтверждённую сверку недостоверной. Отмена защищённого перевода также отклоняется с `RECONCILED_TRANSACTION_IMMUTABLE`; исправление выполняется новой явной компенсирующей операцией, а не изменением исторического transfer.

## Preview, concurrency и idempotency

Preview возвращает рассчитанный баланс, `difference = statement_balance - calculated_balance`, кандидатов и SHA-256 `preview_token`. Fingerprint включает workspace/account, версию счёта, входные данные, cutoff, рассчитанный баланс и точные версии эффективных операций.

Confirm:

1. блокирует счёт и затрагиваемые операции;
2. повторно вычисляет preview;
3. отклоняет stale account version или token с HTTP 409;
4. разрешён только при точном `difference == 0`;
5. одним commit создаёт reconciliation record/items, переводит кандидатов в `reconciled`, обновляет версии, audit и sync outbox.

Уникальный `(workspace_id, idempotency_key)` делает ручной retry безопасным. Тот же ключ с тем же запросом возвращает исходный результат, а с другим запросом даёт `IDEMPOTENCY_CONFLICT`.

`preview_token` — fingerprint состояния, а не credential или security secret. Он обнаруживает stale preview, но авторизация и workspace isolation всегда проверяются backend-контекстом.

## Migration invariants

- Денежные поля используют `NUMERIC(20, 4)`, как accounts и transactions.
- Удаление reconciliation каскадно удаляет только её items. Workspace, account, user и transaction используют ограничивающее поведение FK, чтобы история не теряла ссылки.
- `(workspace_id, idempotency_key)` соответствует scope API retry.
- Индекс `(workspace_id, account_id, statement_date)` обслуживает фильтр и основной порядок истории; индекс items по `transaction_id` обслуживает поиск уже связанных сторон.
- Downgrade сначала удаляет items, затем reconciliation records; принадлежащие таблицам indexes/constraints удаляются вместе с таблицами.

## API

- `POST /api/v1/accounts/{account_id}/reconciliation/preview`
- `POST /api/v1/accounts/{account_id}/reconciliation/confirm`
- `GET /api/v1/accounts/{account_id}/reconciliations`
- `GET /api/v1/accounts/{account_id}/reconciliations/{reconciliation_id}`

Preview и история доступны участнику workspace; confirm требует editor или owner.

## Корректировки

Скрытая adjustment transaction не создаётся. При ненулевой разнице confirm заблокирован. Пользователь должен отдельно исправить операции или создать явную корректировку через обычный financial flow и затем сформировать новый preview. Специализированный adjustment endpoint не добавлен, потому что текущая модель положительной суммы adjustment не выражает безопасно оба знака расхождения.
