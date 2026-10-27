# ADR 0006: Apps Script Bridge как основной Google provider

- Статус: принято
- Дата: 2026-07-22

## Контекст

Google Cloud OAuth project, Client ID и billing недоступны части локальных
пользователей. При этом container-bound Apps Script уже исполняется от имени
пользователя таблицы и может обращаться к локальному backend через временный HTTPS
URL.

## Решение

Основной provider — `apps_script_bridge`. Пользователь вручную создаёт Google-таблицу,
вставляет container-bound Apps Script и один раз передаёт ему публичный backend URL,
binding ID и secret. Apps Script использует свой default Google Cloud project;
backend не вызывает Google Sheets/Drive API и не хранит Google OAuth tokens.

Bridge использует существующие `google_sheet_bindings`, outbox, inbox, conflicts и
sync runs. `google_connection_id` и spreadsheet metadata до регистрации nullable.
Secret показывается только один раз, а backend хранит только SHA-256-derived HMAC key.

Транспорт:

1. приложение пишет финансовое изменение и outbox в одной транзакции;
2. Apps Script получает leased batch через HMAC `pull`;
3. запись остаётся `processing` до HMAC `ack` и возвращается после истечения lease;
4. редактирование Sheets приходит пакетным HMAC `push` в существующий inbox pipeline;
5. reconciliation получает компактный snapshot и никогда молча не удаляет PostgreSQL.

Google OAuth/REST provider сохраняется как необязательный адаптер
`google_oauth`, включаемый только через `GOOGLE_OAUTH_ENABLED=true`. Его worker не
забирает Bridge-события.

## Последствия

- Google Cloud Console, OAuth Client ID, Drive API и Sheets API не нужны основному flow.
- Для Apps Script требуется временный или постоянный публичный HTTPS URL.
- PostgreSQL остаётся источником истины.
- Установка template и применение outbox зависят от квот Apps Script и выполняются
  небольшими пакетами.
- Потерянный одноразовый secret требует rotation; plaintext восстановить невозможно.
