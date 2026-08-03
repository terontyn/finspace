# Google Sheets без Google Cloud Console

Для основного сценария не нужны billing, Google Cloud project, OAuth Client ID или Client
Secret. Нужны работающий Финпространство, Redis и публичный временный HTTPS URL до backend.

1. Установите `GOOGLE_SYNC_PROVIDER=apps_script_bridge`, отключите OAuth и укажите
   `PUBLIC_BACKEND_URL`.
2. Выполните `make google-config-check`. OAuth credentials могут оставаться пустыми.
3. В приложении откройте **Google Sheets** и нажмите **Создать binding**.
4. Сохраните показанные Backend URL, Binding ID и secret. Secret повторно получить нельзя;
   при потере поверните его.
5. Нажмите **Получить Apps Script**. Создайте в редакторе Apps Script все `.gs` files и
   замените manifest содержимым `appsscript.json`.
6. Вручную создайте пустую Google-таблицу и откройте **Расширения → Apps Script**. Этот
   container-bound script автоматически использует свой default project.
7. Запустите `setupFinspace()` и подтвердите разрешения. Будут созданы 13 листов, hidden
   meta/list sheets, headers, filters, validations, named ranges и защиты.
8. Запустите `configureConnection()`. Вставьте URL, Binding ID и secret; скрипт
   зарегистрирует текущий spreadsheet и отправит heartbeat.
9. Через меню **Финпространство → Установить триггеры** выберите интервал. Устанавливаются
   installable onEdit, плановая sync-задача, onOpen и по желанию nightly reminder.
10. Нажмите **Получить обновления**. Initial export поступит через pull и завершится после
    ACK. В приложении binding перейдёт из `initializing` в `active`.

Для локального backend обычный `localhost` недоступен из Apps Script. Используйте только
доверенный временный HTTPS tunnel, не публикуйте Adminer/PostgreSQL/Redis и остановите
tunnel после проверки. При смене URL заново выполните `configureConnection()`.

Повседневная работа: редактирование ставит строку в локальную очередь; scheduled sync
сначала отправляет batch, затем получает backend changes и heartbeat. Физическое удаление
строки Google Sheets не удаляет PostgreSQL record. Для контроля используйте **Полная
сверка**.

Ротация: сначала поверните secret в приложении, затем выберите в меню таблицы **Повернуть
секрет** и вставьте новое значение. Для другой таблицы подтвердите rebind на стороне
приложения. Старый secret прекращает действовать сразу.
