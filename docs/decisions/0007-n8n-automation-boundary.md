# ADR 0007: n8n только как оркестратор Backend API

- Статус: принято
- Дата: 2026-08-03

## Контекст

Проекту нужны расписания, Telegram long polling и доставка уведомлений, но перенос
финансовых правил в workflow создаёт второй источник истины и открывает прямой путь к БД.

## Решение

n8n запускается локально отдельным контейнером в изолированной Compose-сети и видит
только Backend. PostgreSQL и Redis nodes исключены. Workflow передают идентификаторы,
время и idempotency key, а Backend загружает сохранённые правила, проверяет workspace,
выполняет доменную транзакцию и атомарно пишет audit/outbox.

Для machine-to-machine вызовов используется отдельный `ServiceKey` с workspace scope и
минимальными permissions. Ключ хранится в БД только как hash и в n8n как зашифрованный
credential. Telegram bot token также хранится только в n8n.

## Последствия

- повтор workflow не создаёт финансовые дубли благодаря `automation_runs` и доменным
  unique constraints;
- компрометация n8n не раскрывает DB/Redis/Google/user credentials и ограничена выданными
  permissions;
- n8n volume и encryption key требуют отдельного recovery-процесса;
- n8n недоступен публично, поэтому Telegram работает через long polling;
- подтверждение month close и потенциально опасные решения остаются за пользователем.

Отклонён прямой SQL из n8n: он обходит workspace isolation, audit, validation и outbox.

