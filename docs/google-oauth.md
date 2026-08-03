# Google OAuth 2.0

Google — отдельная интеграция, а не способ входа в Финпространство. Backend использует Web
Server flow, одноразовый `state` (10 минут), PKCE S256 и offline access.

## Scopes

```text
openid
email
profile
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/drive.file
```

Полный Drive scope не запрашивается. Access/refresh tokens хранятся только как AES-256-GCM
ciphertext с версией ключа и purpose-bound AAD. Ключ остаётся во внешнем environment.

## Google Cloud Console

1. Создайте отдельный project и настройте OAuth consent screen.
2. Для режима Testing добавьте свой Google account как test user.
3. Включите Google Sheets API и Google Drive API.
4. Создайте OAuth client типа **Web application**.
5. Добавьте redirect URI
   `http://localhost:8000/api/v1/integrations/google/callback`.
6. Заполните `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` и случайный 32-byte base64url
   `GOOGLE_TOKEN_ENCRYPTION_KEY` в локальном `.env`.
7. Перезапустите backend и sync-worker, затем вызовите connect из UI.

Не переносите client secret или encryption key в документацию, Google-книгу, Apps Script
или backup. Endpoint `disconnect` останавливает интеграцию локально; `revoke` дополнительно
отзывает grant в Google и стирает token ciphertext.

```text
GET  /api/v1/integrations/google/status
POST /api/v1/integrations/google/connect
GET  /api/v1/integrations/google/callback
POST /api/v1/integrations/google/disconnect
POST /api/v1/integrations/google/revoke
```

Перед живым OAuth выполните `make google-config-check`, затем следуйте
[runbook приёмки](google-live-acceptance.md). Диагностика не печатает значения
credentials. В acceptance-report попадают только scopes, статусы и признаки наличия
ciphertext; OAuth code и token values запрещены.
