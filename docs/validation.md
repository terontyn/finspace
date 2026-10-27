# Автоматические проверки
PostgreSQL workflow перенесён из отдельной validation-ветки и адаптирован к main: Alembic head 0017_categorization_history, PostgreSQL 17, Redis 8, тестовый runner, focused suites и полный backend suite. Данные и ключи в workflow только одноразовые тестовые fixtures.

Девять старых validation-веток не сливаются целиком. Их прикладные файлы в значительной части уже есть в main, а старые forecast tests теряют исправление REFERENCE_NOW и новые time-injection tests. Поэтому текущие tests сохраняются, workflow объединены без отката кода.

Успех workflow на PR не заменяет release gates, frontend проверки и операторские доказательства off-host backup/clean restore. См. [release.md](release.md).
