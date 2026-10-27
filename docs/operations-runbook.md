# Эксплуатация собственной установки
Эта инструкция использует только обезличенные примеры. Свои адреса, SSH-параметры и настройки храните вне Git.

## Установка и обновление
Используйте [production-install.md](production-install.md), [upgrade.md](upgrade.md) и точный релизный SHA. Production topology — основной Compose плюс compose.production.yml через finspace-compose. Не публикуйте PostgreSQL, Redis, Adminer и n8n в Интернет.

## Проверка состояния
```sh
sudo finspace-compose ps
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
sudo finspace-compose run --rm --no-deps backend alembic current
systemctl status finspace-backup.timer
```

Никогда не вставляйте содержимое .env, токены, cookie и дампы БД в issue или PR. Диагностические артефакты должны быть обезличены.

## Резервное копирование и восстановление
См. [backup-and-restore.md](backup-and-restore.md) и [disaster-recovery-drill.md](disaster-recovery-drill.md). Успешная запись файла не равна проверенному восстановлению. Требуются отдельный домен отказа, проверка manifest/checksum и пробное восстановление в чистом контуре. Не восстанавливать поверх рабочей БД без отдельного плана.

## Приёмка
[release.md](release.md) задаёт machine gates и evidence. Для тестов используется только отдельная TEST_DATABASE_URL с защитой runner. Состояние production, SSH и Google identifiers не являются частью публичного репозитория.
