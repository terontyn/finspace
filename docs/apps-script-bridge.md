# Apps Script Bridge

`apps_script_bridge` — основной provider Google Sheets. Он работает без Google Cloud
project, Client ID, Client Secret, Drive API и Google OAuth tokens на backend. Пользователь
сам создаёт пустую Google-таблицу; привязанный к ней Apps Script использует собственный
default project и обращается к публичному HTTPS URL backend.

PostgreSQL остаётся источником истины. Таблица — редактируемое представление и транспорт
изменений, а не резервная база данных.

## Конфигурация

```env
GOOGLE_SYNC_PROVIDER=apps_script_bridge
GOOGLE_OAUTH_ENABLED=false
APPS_SCRIPT_BRIDGE_ENABLED=true
PUBLIC_BACKEND_URL=https://temporary-public-host
APPS_SCRIPT_PULL_BATCH_SIZE=100
APPS_SCRIPT_HEARTBEAT_TTL_MINUTES=15
```

Для локального backend нужен временный HTTPS tunnel. `PUBLIC_BACKEND_URL` содержит только
origin без `/api/v1/...`. Проверка:

```powershell
make google-config-check
```

При выбранном Bridge проверяются provider, feature flag, HTTPS URL, окно HMAC, Redis и
общий sync flag. OAuth credentials и token encryption key не требуются.

## Binding и регистрация

JWT пользователя применяется только к управлению binding:

```text
POST   /api/v1/google-sheets/apps-script/binding
GET    /api/v1/google-sheets/apps-script/binding
POST   /api/v1/google-sheets/apps-script/binding/rotate-secret
POST   /api/v1/google-sheets/apps-script/binding/pause
POST   /api/v1/google-sheets/apps-script/binding/resume
DELETE /api/v1/google-sheets/apps-script/binding
GET    /api/v1/google-sheets/apps-script/package
```

Создание и ротация показывают plaintext secret один раз. PostgreSQL хранит только
SHA-256-derived HMAC key. Для привязки другой таблицы нужна ротация с `rebind=true`;
регистрация той же таблицы идемпотентна.

## HMAC transport

Apps Script не получает JWT. `register`, `push`, `pull`, `ack`, `reconcile` и `heartbeat`
требуют заголовки:

```text
X-Finspace-Binding-ID
X-Finspace-Timestamp
X-Finspace-Nonce
X-Finspace-Body-SHA256
X-Finspace-Signature
```

Подпись — hex HMAC-SHA256 над строкой
`timestamp + "\n" + nonce + "\n" + body_sha256`; ключ — SHA-256 от binding secret.
Backend сверяет точное тело, допустимое время, одноразовый Redis nonce, binding status,
workspace и spreadsheet ID. Secret нельзя помещать в ячейки, логи или исходники.

## Pull, lease и ACK

После регистрации backend создаёт `full_export` outbox events. `pull` выдаёт ограниченный
batch canonical rows и ставит lease, но не завершает события. Apps Script применяет строки
по техническому `_id` и отправляет ACK. Только ACK переводит event в `completed`; после
ACK всех событий initial export binding становится `active`. Неподтверждённый event снова
доступен после окончания lease. Повторный ACK безопасен.

Backend changes используют тот же outbox. Старый `sync-worker` выбирает только binding с
provider `google_oauth` и не может забрать события Bridge.

## Push и reconciliation

Installable onEdit лишь ставит строку в Document Properties queue и помечает `DIRTY`.
Плановый trigger отправляет до 100 событий одним `push`; обработка сохраняет inbox,
idempotency, optimistic locking, audit, нормализацию и conflicts финансового ядра.

### Локальная очередь и состояния

Document Properties queue — активный журнал доставки, а event ID дополнительно хранится в
note ячейки статуса. Note не содержит финансовых данных или secret и позволяет восстановить
очередь после повреждения/потери property. Backend inbox остаётся окончательным журналом
идемпотентности.

```text
user edit -> DIRTY + durable event ID + queued
queued -> PENDING -> signed push
  applied/duplicate + canonical row -> SYNCED + dequeue
  conflict                         -> CONFLICT + dequeue
  confirmed rejected event         -> ERROR + dequeue
  network/transient/protocol error -> PENDING + remains queued
  auth/configuration error         -> ERROR + remains queued until configuration is fixed
```

Удаление выполняется по паре `row key + event ID`, а не только по номеру строки. Поэтому
ответ старой HTTP-попытки не может удалить более новое редактирование той же строки.
`applied` и `duplicate` считаются подтверждением только при наличии валидной canonical row.
Повтор запроса сохраняет business event ID, но подписывается новым timestamp/nonce.

| Класс ошибки | Состояние строки | Очередь |
|---|---|---|
| DNS, timeout, connection reset | `PENDING` | сохраняется |
| HTTP 408/425/429/500/502/503/504 | `PENDING` | сохраняется |
| replay nonce | `PENDING` | сохраняется; новый HTTP retry получает новый nonce |
| неверная подпись/configuration | `ERROR` | сохраняется до исправления настройки |
| невалидный или неполный 2xx response | `PENDING` | сохраняется |
| подтверждённый per-item validation reject | `ERROR` | удаляется как terminal |
| version conflict | `CONFLICT` | удаляется как terminal |

Все read-modify-write операции queue выполняются под Document Lock. Долгий HTTP-запрос
идёт без Document Lock; отдельный Script Lock не допускает два одновременных push. Перед
отправкой и после ответа event ID сверяется повторно. Изменение, возникшее во время HTTP,
получает новый event ID и остаётся `DIRTY`.

Перед каждым push выполняется recovery scan строк `DIRTY`/`PENDING`. Если property item
исчез, он восстанавливается с тем же event ID из note. Для legacy `PENDING` новой строки без
entity ID и без event note автоматический retry запрещён: невозможно доказать отсутствие
уже применённого создания; строка переводится в `ERROR` с требованием полной сверки.
Повреждённый JSON сохраняется в ограниченной quarantine property и перестраивается по
маркерам строк. Переполнение queue больше не отбрасывает старые события через `slice`.

Сверка отправляет пакетами компактные элементы `entity_type`, `entity_id`, `version`,
`row_hash`, `row_number`, `sync_status`. Backend классифицирует matched/missing/duplicate,
unknown, tamper, newer и conflict; восстановление выполняется новыми outbox events.

## Наблюдаемость

Статус API показывает регистрацию таблицы, pending/failed/conflicts, последние pull, ACK и
heartbeat. Heartbeat считается здоровым только в пределах
`APPS_SCRIPT_HEARTBEAT_TTL_MINUTES`. Audit фиксирует создание, регистрацию, pull, ACK,
heartbeat, ротацию, паузу и сверку без secret values.

OAuth/Sheets API реализация сохранена как необязательный provider `google_oauth` и видна в
UI только при `GOOGLE_OAUTH_ENABLED=true`.

## Автоматические проверки Apps Script

Минимальный deterministic harness использует встроенный Node test runner и моки
PropertiesService, LockService, SpreadsheetApp и UrlFetchApp:

```powershell
node --test google-apps-script/tests/queue-reliability.test.cjs
```

Он проверяет DNS/timeout/503, невалидный 2xx, auth/config failure, replay с новым nonce,
partial batch, concurrent edit, recovery по event note и duplicate retry после потерянного
ответа.
