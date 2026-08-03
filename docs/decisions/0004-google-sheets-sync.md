# ADR 0004: основная Google-книга и безопасная синхронизация

- Статус: принято
- Дата: 2026-07-22

## Контекст

PostgreSQL уже содержит проверенное финансовое ядро, auth, audit, staging import и
backup. Google Sheets должен стать обязательной рабочей книгой и дополнительным
редактируемым интерфейсом, не превращаясь во вторую независимую базу данных.

## Источник истины и потоки

PostgreSQL остаётся единственным источником финансовой истины. Исходящие изменения
попадают в transactional `sync_outbox` в той же транзакции, что сущность и audit.
Отдельный worker вызывает Google API; недоступность Google не откатывает финансовую
операцию.

Входящие изменения принимаются только HMAC-authenticated webhook, записываются в
идемпотентный `sync_inbox`, повторно валидируются backend и применяются с optimistic
locking. Устаревшая версия создаёт `sync_conflicts`, но не перезаписывает PostgreSQL.

## OAuth и credentials

Используется Google OAuth 2.0 Web Server flow со state, PKCE S256, offline access и
минимальными scopes: `openid`, `email`, `profile`, Sheets и `drive.file`. OAuth-подключение
не заменяет auth Финпространства. Callback связывается с текущей refresh-сессией,
пользователем и workspace; state хранится только как hash и одноразово потребляется.

Access/refresh tokens и PKCE verifier шифруются AES-256-GCM application-level ключом из
environment. Ciphertext содержит nonce и key version; токены никогда не возвращаются
frontend, audit или логам. Смена версии ключа допускает постепенную перешифровку.

## Google-книга

Один workspace имеет не более одной незакрытой binding. Шаблон v1 создаёт все требуемые
листы; реально синхронизируются `Операции`, `Счета`, `Категории`. Технические UUID,
workspace, version, canonical SHA-256 row hash и timestamps скрываются и защищаются.
`GoogleSheetTemplateMigrator` отделяет будущие миграции v1 → v2 от пересоздания книги.

Книга создаётся в режиме `push_only`. `bidirectional` включается вручную только после
установки Apps Script, выдачи одноразового binding secret и настройки HTTPS webhook URL.

## Webhook authentication

Apps Script хранит отдельный secret в Script Properties. Backend хранит только HMAC-SHA256
hash. Подпись покрывает binding ID, timestamp, nonce и SHA-256 тела. Timestamp имеет
ограниченное окно, nonce одноразово резервируется в Redis. Пользовательский JWT, Google
refresh token, пароль и DB credentials в Apps Script отсутствуют.

## Row hash и reconciliation

Canonical JSON сортирует ключи, нормализует whitespace, UTC timestamps, UUID и Decimal
без лишних нулей. Reconciliation сравнивает UUID/version/hash/deleted state и технические
поля. Разрешено автоматически восстановить отсутствующее представление, дубли и
технические колонки. Нельзя автоматически принять конфликт, неизвестную сущность или
удалить PostgreSQL-запись из-за исчезнувшей строки.

## Удаление и конфликты

Физическое удаление строки Google Sheets не удаляет сущность PostgreSQL. Отмена/архив
выполняются доменным полем или приложением. Конфликт хранит минимальные безопасные
snapshots и разрешается `keep_database`, `keep_sheet` или `manual_merge` через frontend.

## Последствия

- Google API полностью изолирован за mockable client и не вызывается обычными тестами;
- очередь и retry сохраняются в PostgreSQL, Redis используется лишь для replay/coordination;
- backups содержат ciphertext, но не encryption key, поэтому без внешнего ключа Google
  credentials после restore недоступны;
- push-only работает без публичного webhook; Apps Script требует временный HTTPS tunnel;
- arbitrary spreadsheets, несколько книг, n8n, Telegram и production deployment остаются
  вне этапа 4.
