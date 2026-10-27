# Финпространство Apps Script v1

Container-bound Apps Script реализует основной provider `apps_script_bridge` без Google
Cloud OAuth. Он хранит binding secret только в Document Properties, подписывает каждый
запрос HMAC и не содержит JWT, database credentials или Google tokens.

## Установка

1. В приложении создайте binding и сохраните одноразовый secret.
2. Вручную создайте пустую Google-таблицу и откройте **Расширения → Apps Script**.
3. Создайте файлы `Code.gs`, `Config.gs`, `Template.gs`, `EditQueue.gs`, `SyncClient.gs`,
   `Validation.gs`; вставьте содержимое из этой директории.
4. Включите отображение manifest и замените `appsscript.json`.
5. Запустите `setupFinspace()` и подтвердите spreadsheet/UI/external request/trigger scopes.
6. Вернитесь в таблицу и перезагрузите страницу: `onOpen()` добавит меню
   **Финпространство** в контексте интерфейса книги.
7. Выберите **Финпространство → Настроить подключение**, укажите HTTPS backend URL,
   Binding ID, secret и интервал.
8. Через меню установите triggers и нажмите **Получить обновления** для initial export.

Apps Script использует default project таблицы. Google Cloud Console, billing и OAuth
Client ID не нужны. Локальный backend должен быть доступен через временный доверенный HTTPS
tunnel; обычный localhost из Apps Script недоступен.

## Поведение

Installable onEdit не выполняет HTTP: он отмечает строку `DIRTY` и кладёт устойчивый event
в Document Properties queue. Time-driven sync отправляет batch, получает canonical rows,
применяет их по `_id`, отправляет ACK и heartbeat. Reconciliation передаёт компактный
snapshot пакетами.

Event удаляется из локальной очереди только после валидного terminal ответа backend.
DNS/timeout/502/503/504, replay nonce, неверная подпись и повреждённый HTTP response не
удаляют business event. Каждый HTTP retry получает новый HMAC timestamp/nonce, сохраняя
исходный event ID для backend idempotency.

Queue read-modify-write защищён Document Lock, но lock не удерживается во время HTTP.
Event ID дублируется в note ячейки статуса: periodic recovery восстанавливает пропавший
property item для `DIRTY`/`PENDING`. Ответ старой попытки удаляет только совпадающий event ID
и не может потерять новое редактирование той же строки.

Secret после backend rotation обновляется через меню **Повернуть секрет**. Reset очищает
только Document Properties и локальную очередь; backend binding и PostgreSQL records не
удаляются.

Проверка deterministic queue harness:

```powershell
node --test google-apps-script/tests/queue-reliability.test.cjs
```
