# Apps Script v1

Исходники находятся в `google-apps-script/` и доступны авторизованному пользователю через
`GET /api/v1/google-sheets/apps-script/package`. Создайте одноимённые files в
**Расширения → Apps Script** и замените manifest.

Публичные функции:

```javascript
setupFinspace()
configureConnection()
registerBinding()
installTriggers()
removeTriggers()
pullChanges()
pushPendingChanges()
runReconciliation()
showConnectionStatus()
resetLocalConfiguration()
```

`setupFinspace()` идемпотентно устанавливает template v1 в активной книге и не требует
Sheets API на backend. `configureConnection()` хранит Backend URL, Binding ID и secret в
Document Properties, регистрирует spreadsheet и проверяет heartbeat. Secret никогда не
пишется в ячейки.

Installable onEdit вызывает `captureFinspaceEdit`: он не делает HTTP, а помечает строку
`DIRTY` и сохраняет устойчивый event ID в локальной очереди. Time-driven trigger с
интервалом 1/5/10/15/30 минут выполняет batch push, pull+ACK и heartbeat. Nightly trigger
лишь ставит reminder о сверке.

Меню книги содержит настройку, триггеры, ручные push/pull, сверку, статус, обновление
повёрнутого secret, удаление триггеров и локальный reset. Ротация backend secret делается в
приложении; затем новое значение вводится в книге.

Apps Script подписывает точное JSON body и передаёт обязательный
`X-Finspace-Body-SHA256`. При pull строки применяются по `_id`, после чего отправляется ACK.
Снимок reconciliation передаётся пакетами по 500 строк. Детали протокола — в
[Apps Script Bridge](apps-script-bridge.md).
