# Основная Google-книга

Основной способ синхронизации — [Apps Script Bridge](apps-script-bridge.md). Пользователь
вручную создаёт пустую таблицу, а `setupFinspace()` устанавливает template v1. Google Cloud
OAuth не обязателен; backend не вызывает Google Sheets/Drive API и не хранит Google tokens.

Template v1 содержит 13 листов:

```text
Сегодня, Операции, Счета, Категории, Бюджет, Цели, Долги, Импорт,
Ошибки, Конфликты, Инструкция, _sync_meta, _lists
```

`_sync_meta` и `_lists` скрыты. Технические UUID/version/hash columns скрыты и защищены.
Шаблон добавляет frozen headers, filters, number formats, named ranges, validations и
conditional formatting статусов `SYNCED`, `DIRTY`, `PENDING`, `CONFLICT`, `ERROR`,
`DELETED`, `TAMPER`.

PostgreSQL — источник истины. Backend формирует canonical rows, а Apps Script применяет их
по `_id`. Удаление строки из Sheets не удаляет финансовую запись. Изменения технических
полей считаются tamper и требуют сверки.

Установка без Cloud Console описана в
[отдельной инструкции](google-without-cloud-console.md). Прежний OAuth provider сохранён
для окружений, где он уже настроен: включите `GOOGLE_OAUTH_ENABLED=true` и выберите
`GOOGLE_SYNC_PROVIDER=google_oauth`.
