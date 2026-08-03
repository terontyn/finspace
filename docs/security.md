# Модель безопасности

## n8n и ServiceKey

n8n опубликован только на loopback и подключён к изолированной сети с Backend, но не с
PostgreSQL/Redis. Встроенные DB/Redis, command и filesystem nodes исключены. Machine auth
отделён от user JWT: `Authorization: ServiceKey <secret>` проверяет hash, prefix, срок,
revoke, workspace scope и конкретное permission. Secret показывается только при создании
или rotation и не хранится plaintext в БД.

`N8N_ENCRYPTION_KEY` находится только в локальном `.env`; ServiceKey и bot token — только
в n8n encrypted credentials. Workflow exports, audit и логи не должны содержать эти
значения. Все automation mutations требуют `X-Idempotency-Key`, а финансовое состояние
меняет только Backend. Полная модель: [automation-security.md](automation-security.md).

## Apps Script Bridge

Основной Google provider не использует Google OAuth: backend не получает и не хранит
Google access/refresh tokens и не обращается к Drive/Sheets API. Binding secret показывается
один раз и хранится в PostgreSQL только как SHA-256-derived HMAC key; plaintext находится
только в Document Properties конкретной книги.

Bridge endpoints работают без пользовательского JWT, но требуют binding ID, timestamp,
Redis nonce, SHA-256 точного body и HMAC signature. Сверяются active/paused state,
workspace и зарегистрированный spreadsheet ID. Nonce живёт дольше допустимого clock skew;
сравнение body hash/signature выполняется constant-time. Логи и audit не содержат secret,
request body финансовых строк или HMAC headers.

Для локального backend нужен временный HTTPS tunnel. Публиковать разрешено только HTTP API;
PostgreSQL, Redis и Adminer остаются на loopback/private network. После приёмки tunnel и
Apps Script triggers отключаются.

## Пароли и вход

Пароль имеет длину 10–128 символов и хэшируется Argon2id. Plaintext не сохраняется,
не возвращается API, не логируется и не попадает в audit. Ответ для неизвестного email и
неверного пароля одинаков: `INVALID_CREDENTIALS`.

Redis хранит временные счётчики по HMAC нормализованного email и IP. Повторные ошибки
дают временную блокировку; успешный вход сбрасывает счётчики. В `users` остаются только
счётчик неудач и срок lock, а не IP или пароль.

## Test и acceptance isolation

`TESTING=true` никогда не допускается вместе с `ENVIRONMENT=production`. Pytest,
test seed, migration cycle и cleanup проверяют test-name и marker до destructive
действий; подробности — в [test-database-safety.md](test-database-safety.md).

Live acceptance работает только в development и хранит локальный registry по
точному `acceptance_run_id`. Registry/report исключены из Git и не содержат password,
cookie, OAuth code, tokens, client secret, encryption key, HMAC secret или row
payload. Cleanup требует явный список объектов и exact workspace/user IDs.

## Token lifecycle

1. Register/login создаёт короткоживущий HS256 access JWT и случайный refresh token.
2. Access token возвращается JSON и живёт только в памяти frontend.
3. Refresh token устанавливается `HttpOnly`, `SameSite=Lax`, path `/`. Корневой path
   нужен, потому что UI вызывает API через configurable same-origin reverse-proxy prefix;
   JavaScript всё равно не видит cookie.
4. В PostgreSQL хранится SHA-256 hash refresh secret; IP/User-Agent — только HMAC hash.
5. Refresh одноразово ротирует token/session и связывает старую с новой.
6. Повтор отозванного заменённого token означает reuse и отзывает активные сессии.
7. Logout отзывает текущую сессию; logout-all — все сессии пользователя.

Локально cookie имеет `Secure=false`; production-конфигурация автоматически требует
`Secure=true`. Refresh/logout также сверяют `Origin` с `CORS_ORIGINS`, если браузер его
передал. SameSite=Lax блокирует cross-site POST cookie; это принятая CSRF-модель этапа 3.

## Endpoints

```text
POST /api/v1/auth/register
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout
POST /api/v1/auth/logout-all
GET  /api/v1/auth/me
POST /api/v1/auth/set-development-password
```

## Workspace и роли

- viewer: чтение счетов, категорий, операций и summary;
- editor: viewer + изменение финансовых данных, audit, импорт и rollback;
- owner: editor + управление участниками и критические действия.

Access JWT определяет user. Опциональный `X-Workspace-ID` лишь выбирает пространство из
его membership. Workspace из request body не считается доверенным. `X-User-ID` работает
только при явном development feature flag и всегда запрещён в production.

## Секреты и аудит

`JWT_SECRET_KEY` вне development должен быть длинным случайным значением, отличным от
example/default. Audit фиксирует register/login/logout, отзыв сессии, import и
backup/restore verification, но не содержит пароль, token, cookie, полный import-файл
или полный финансовый payload.

Google OAuth использует одноразовый state, PKCE S256 и минимальные scopes. Access/refresh
tokens зашифрованы AES-256-GCM application key с версией; plaintext не возвращается UI и не
попадает в log/audit. Apps Script не получает JWT/Google token: webhook использует отдельный
binding secret, показанный один раз. Backend хранит только SHA-256-derived HMAC key, проверяет
timestamp и constant-time signature, а nonce резервирует в Redis против replay.

`drive.file` ограничивает доступ файлами, созданными/выбранными приложением. `_sync_meta` и
технические колонки не содержат secrets. Workspace и spreadsheet берутся из binding, а не из
недоверенного row payload. Технический tamper отклоняется и аудитируется.

Ограничения этапа: нет TLS termination, MFA/WebAuthn, email reset, managed secret manager и
шифрования backups. Telegram использует long polling; n8n и его UI остаются локальными.
Временный tunnel предназначен только для ручного HMAC smoke test. До
production perimeter публичное развёртывание запрещено.
