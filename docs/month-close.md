# Hard Month Close

Month Close — жёсткое бухгалтерское закрытие периода. PostgreSQL остаётся источником
истины, а `month_close_controls.closed_through` хранит накопительную границу workspace.
Любая финансовая mutation с effective date не позднее этой даты завершается
`HTTP 409 MONTH_CLOSED`; автоматического открытия периода нет.

## Состояния и хронология

```text
not_prepared -> blocked | ready
blocked      -> prepare -> blocked | ready
ready        -> confirm -> confirmed
confirmed    -> owner reopen -> reopened
reopened     -> prepare -> blocked | ready
```

- `prepare` доступен editor/owner; viewer запрещён.
- `confirm` и `reopen` доступны только owner.
- Текущий и будущий месяц нельзя подготовить или подтвердить:
  `MONTH_CLOSE_PERIOD_NOT_ENDED`.
- После первого закрытого завершённого месяца следующие месяцы закрываются строго
  последовательно. Пустой промежуточный месяц является полноценным периодом.
- Открыть можно только последний confirmed month, чей конец равен `closed_through`.
  Более ранние месяцы открываются в обратном порядке.
- `reopen` требует непустую причину и никогда не удаляет historical revision.
- После reopen прямой confirm запрещён: требуется новый prepare.

`confirm` и `reopen` требуют `X-Idempotency-Key`. Повтор ключа с тем же semantic payload
возвращает сохранённый первоначальный результат, даже если closure позже изменился.
Повтор ключа с другим payload возвращает
`MONTH_CLOSE_IDEMPOTENCY_CONFLICT` и не создаёт второй revision/audit event.

## Prepare, confirm и concurrency

Prepare создаёт или блокирует только строку `month_closures` на короткое обновление,
строит preview, issues, financial fingerprint и отдельный `prepare_token`. Долгий расчёт
не держит глобальную financial write-блокировку. Поэтому запись во время или после
prepare допустима; confirm повторно рассчитывает состояние и отвечает
`MONTH_CLOSE_PREVIEW_STALE`, если preview изменился.

Confirm и все ledger-affecting write paths используют единый порядок блокировок:

```text
month_close_controls FOR UPDATE
-> month_closures FOR UPDATE (для close/reopen)
-> domain entity locks
-> mutation/revision/audit
-> commit
```

Под `READ COMMITTED` строка control сериализует confirm и финансовые mutations. Confirm
атомарно создаёт immutable revision, обновляет closure, переносит `closed_through` и
пишет audit. Ошибка не оставляет частичный close.

## Snapshot и fingerprint

Каждый successful confirm создаёт новую immutable строку `month_close_revisions`.
Snapshot содержит aggregates по валютам, balances as-of конца периода, метаданные
периода, issues, actor/time, revision number и SHA-256 fingerprint. Полная копия ledger
не сохраняется. Старый `month_closures.summary` остаётся current preview/latest summary
для совместимости.

Canonical fingerprint учитывает финансовые поля счетов и операций до cutoff, splits и
refund/original relation. Decimal кодируется без float, коллекции сортируются
детерминированно. `confirmed` и `reconciled` нормализуются в один effective status.
Cosmetic account/category metadata, update timestamps, reconciliation evidence и
outbox delivery state не входят в fingerprint.

Legacy confirmed closures получают revision с существующим summary,
`legacy_unverified=true` и `financial_fingerprint=NULL`; миграция не придумывает
исторический hash. Для новых revisions fingerprint обязателен.

## Issues

Каждая issue имеет единый безопасный UI-контракт: `code`, `severity`, `scope`, `count`,
локализуемое `message` и минимальные `details`. В `details` не попадают exception text,
секреты или произвольный внешний payload. Severity имеет ровно три значения:
`blocker`, `warning`, `info`.

Blockers не позволяют confirm: незавершённый период, draft transactions в closing
interval, sync conflict с финансовым влиянием на cutoff, ошибка последовательности и
unhealthy backup только при policy `require_healthy`. Если дату предлагаемой финансовой
mutation из sync conflict безопасно определить нельзя, конфликт консервативно относится
к closing interval; известный конфликт вне периода не блокирует весь workspace.
Backup policy по умолчанию — `warn`.

Warnings не блокируют close: uncategorized/possible duplicate transactions, отрицательные
остатки, отсутствие reconciliation, failed recurring executions/outbox, import rows,
требующие внимания, и missing/unverified/stale backup при policy `warn`.

Info: staged imports, out-of-period sync conflicts и отсутствие финансовой активности.
Failed outbox является warning, а staging сам по себе не является ledger. Credit account
без statement-cycle metadata также не превращается в blocker.

## Reconciliation coverage

Month Close не подтверждает сверку и не меняет transaction status. Он только читает
evidence отдельного Account Reconciliation bounded context и сохраняет рассчитанное
coverage в revision snapshot.

- eligible account имеет эффективную активность в периоде или ненулевой остаток на конец;
- нулевая активность вместе с нулевым остатком не создаёт ложное предупреждение;
- `statement_date >= period end` считается достаточным evidence, включая более позднюю
  дату выписки;
- archived account остаётся в coverage, если он влияет на исторический баланс;
- отсутствие достаточного confirmed evidence даёт `ACCOUNT_NOT_RECONCILED` уровня
  warning, а не blocker.

Coverage содержит display-safe snapshot имени, типа, валюты, period-end balance,
причину eligibility и даты evidence. Это описание состояния на момент confirm, а не
команда для reconciliation domain.

## Backup policy и честное состояние

Month Close не запускает и не настраивает backup. API и UI показывают одно из четырёх
операционных состояний: `missing`, `unverified`, `stale`, `healthy`.

- `backup_policy=warn`: первые три состояния являются warning и допускают осознанный
  confirm владельцем;
- `backup_policy=require_healthy`: любое состояние кроме `healthy` является blocker;
- отсутствие подтверждённой restore-проверки никогда не отображается как защищённый
  backup.

## Read API, history и reports

`GET /api/v1/month-close?limit=120` остаётся совместимым и дополнительно возвращает
`closed_through`, `backup_policy` и компактный список периодов текущего года плюс реально
существующую историю. Элементы содержат status, version, revision, timestamps, counts и
backend-calculated `capabilities`.

Immutable read API (viewer и выше):

```text
GET /api/v1/month-close/{year}/{month}/history?order=newest|oldest&limit=&offset=
GET /api/v1/month-close/{year}/{month}/history/{revision_number}
GET /api/v1/month-close/{year}/{month}/history/{revision_number}/report
GET /api/v1/month-close/{year}/{month}/history/{revision_number}/comparison
```

History детерминированно сортируется и изолируется по workspace. Mutation endpoints для
revision отсутствуют. Report имеет `mode=as_closed` и строится только из immutable
`revision.snapshot`: он не перечитывает live ledger. Comparison явно разделяет
`as_closed` и `current`, сравнивает totals отдельно по валютам, balances и category
aggregates и никогда не складывает разные валюты.

Legacy revision возвращается с `legacy_unverified=true` и
`financial_fingerprint=null`. Это исторический snapshot, но не криптографически
проверенный снимок.

## UI и права

Экран `/month-close` использует только backend-данные и capabilities:

- viewer видит периоды, issues, history, as-closed report и comparison;
- editor дополнительно выполняет prepare;
- owner выполняет prepare, confirm и latest-only reopen.

Backend permissions остаются authoritative. Confirm и reopen открывают keyboard-usable
modal; reopen требует причину. При `MONTH_CLOSE_PREVIEW_STALE` или
`MONTH_CLOSE_VERSION_CONFLICT` UI не повторяет mutation автоматически, обновляет данные и
требует новое явное действие. Транспортный retry одного и того же intent сохраняет его
`X-Idempotency-Key`; новый intent получает новый ключ.

## Матрица mutations

| Граница | Поведение при effective date `<= closed_through` |
|---|---|
| Transaction create/transfer/split/refund | `MONTH_CLOSED` |
| Transaction update | Проверяются старая и новая даты; `MONTH_CLOSED` для любой закрытой |
| Transaction cancel/delete/restore/confirm | `MONTH_CLOSED` |
| Account create/opening balance/opening date/currency | `MONTH_CLOSED`, если меняется historical balance |
| Account delete/restore | `MONTH_CLOSED`, если account влияет на закрытую историю |
| Account name/icon/color и archive | Разрешено: historical calculations не меняются |
| Category rename/icon/color/order/archive | Разрешено; revision хранит label snapshot |
| Import commit | Полный preflight; весь batch отклоняется до первой записи |
| Import rollback | Полностью отклоняется; reconciled invariant остаётся сильнее close |
| Google inbound / keep_sheet / manual_merge | Terminal `MONTH_CLOSED`, canonical ledger не меняется |
| Google keep_database | Разрешено: ledger не меняется |
| Recurring / Telegram backdated write | Terminal `MONTH_CLOSED`, transaction не создаётся |
| Account reconciliation confirm | Разрешено; `confirmed -> reconciled` не меняет fingerprint |

## Допустимые financial write boundaries

Production-код создаёт `FinancialTransaction` только в двух местах:

- `app.services.transactions` — общий transaction domain service; все обычные API,
  Telegram, recurring и normal Google inbound сходятся сюда;
- `app.services.imports` — атомарный import commit после полного guard preflight.

Google conflict resolution с ledger mutation берёт control lock перед domain locks.
Будущая интеграция обязана использовать transaction service либо явно выполнить тот же
central guard под control lock. Прямой SQL из n8n, Apps Script и connectors запрещён.

## Ручная работа

1. Откройте раздел «Закрытие», выберите завершённый месяц и выполните prepare.
2. Устраните blockers; warnings осознанно просмотрите.
3. Owner подтверждает актуальный preview. Stale preview нужно подготовить заново.
4. Для корректировки закрытого периода owner сначала вручную открывает последний месяц,
   указывает причину и при необходимости повторяет действие в обратном порядке.
5. После изменений каждый открытый месяц снова проходит prepare и confirm по порядку.

Backup не запускается автоматически и не настраивается Month Close. Текущая policy
`warn` только показывает состояние существующего backup-контура.
