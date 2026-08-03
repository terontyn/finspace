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
6. Запустите `configureConnection()`, укажите HTTPS backend URL, Binding ID, secret и
   интервал.
7. Через меню установите triggers и нажмите **Получить обновления** для initial export.

Apps Script использует default project таблицы. Google Cloud Console, billing и OAuth
Client ID не нужны. Локальный backend должен быть доступен через временный доверенный HTTPS
tunnel; обычный localhost из Apps Script недоступен.

## Поведение

Installable onEdit не выполняет HTTP: он отмечает строку `DIRTY` и кладёт устойчивый event
в Document Properties queue. Time-driven sync отправляет batch, получает canonical rows,
применяет их по `_id`, отправляет ACK и heartbeat. Reconciliation передаёт компактный
snapshot пакетами.

Secret после backend rotation обновляется через меню **Повернуть секрет**. Reset очищает
только Document Properties и локальную очередь; backend binding и PostgreSQL records не
удаляются.
