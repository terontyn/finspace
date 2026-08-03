function onOpen() {
  buildFinspaceMenu_();
  if (finspaceProperties_().getProperty(FINSPACE.RECONCILE_REMINDER_KEY) === 'due') {
    SpreadsheetApp.getActive().toast(
      'Рекомендуется выполнить полную сверку.',
      'Финпространство',
      10
    );
    finspaceProperties_().deleteProperty(FINSPACE.RECONCILE_REMINDER_KEY);
  }
}

function installedFinspaceOpen() {
  onOpen();
}

function buildFinspaceMenu_() {
  SpreadsheetApp.getUi()
    .createMenu('Финпространство')
    .addItem('Настроить подключение', 'configureConnection')
    .addItem('Установить триггеры', 'installTriggers')
    .addSeparator()
    .addItem('Отправить изменения', 'pushPendingChanges')
    .addItem('Получить обновления', 'pullChanges')
    .addItem('Полная сверка', 'runReconciliation')
    .addItem('Статус подключения', 'showConnectionStatus')
    .addSeparator()
    .addItem('Повернуть секрет', 'updateRotatedSecret')
    .addItem('Удалить триггеры', 'removeTriggers')
    .addItem('Сбросить локальную конфигурацию', 'resetLocalConfiguration')
    .addToUi();
}

function installTriggers() {
  finspaceConfig_();
  const interval = Number(
    finspaceProperties_().getProperty(FINSPACE.INTERVAL_KEY) || 5
  );
  if (FINSPACE.ALLOWED_INTERVALS.indexOf(interval) < 0) {
    throw new Error('Сохранён недопустимый интервал синхронизации.');
  }
  removeTriggers_(false);
  ScriptApp.newTrigger('captureFinspaceEdit')
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onEdit()
    .create();
  ScriptApp.newTrigger('scheduledFinspaceSync')
    .timeBased()
    .everyMinutes(interval)
    .create();
  ScriptApp.newTrigger('installedFinspaceOpen')
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onOpen()
    .create();
  const ui = SpreadsheetApp.getUi();
  if (ui.alert(
    'Напоминание о сверке',
    'Добавить ежедневное напоминание о полной сверке?',
    ui.ButtonSet.YES_NO
  ) === ui.Button.YES) {
    ScriptApp.newTrigger('nightlyReconciliationReminder')
      .timeBased()
      .atHour(3)
      .everyDays(1)
      .create();
  }
  ui.alert('Триггеры установлены. Синхронизация: каждые ' + interval + ' мин.');
}

function removeTriggers() {
  removeTriggers_(true);
}

function removeTriggers_(notify) {
  const handlers = [
    'captureFinspaceEdit',
    'scheduledFinspaceSync',
    'installedFinspaceOpen',
    'nightlyReconciliationReminder'
  ];
  let removed = 0;
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (handlers.indexOf(trigger.getHandlerFunction()) >= 0) {
      ScriptApp.deleteTrigger(trigger);
      removed += 1;
    }
  });
  if (notify) SpreadsheetApp.getUi().alert('Удалено триггеров: ' + removed + '.');
}

function scheduledFinspaceSync() {
  const lock = LockService.getDocumentLock();
  if (!lock.tryLock(1000)) return;
  try {
    pushPendingChanges_(false);
    pullChanges_(false);
    heartbeat_();
    finspaceProperties_().deleteProperty(FINSPACE.LAST_ERROR_KEY);
  } catch (error) {
    finspaceProperties_().setProperty(
      FINSPACE.LAST_ERROR_KEY,
      String(error).slice(0, 1000)
    );
    throw error;
  } finally {
    lock.releaseLock();
  }
}

function nightlyReconciliationReminder() {
  finspaceProperties_().setProperty(FINSPACE.RECONCILE_REMINDER_KEY, 'due');
}

function showConnectionStatus() {
  const config = finspaceConfig_();
  const heartbeat = heartbeat_();
  const queueSize = queuedFinspaceEdits_().length;
  const lastError = finspaceProperties_().getProperty(FINSPACE.LAST_ERROR_KEY) || 'нет';
  SpreadsheetApp.getUi().alert(
    'Binding: ' + config.bindingId + '\n' +
    'Backend: ' + config.backendUrl + '\n' +
    'Статус binding: ' + heartbeat.binding_status + '\n' +
    'Outbox на backend: ' + heartbeat.pending_outbox + '\n' +
    'Локальная очередь: ' + queueSize + '\n' +
    'Последняя ошибка: ' + lastError
  );
}

function openFinspaceApp() {
  const url = finspaceConfig_().appUrl;
  const html = HtmlService.createHtmlOutput(
    '<script>window.open(' + JSON.stringify(url) + ', "_blank");google.script.host.close();</script>'
  );
  SpreadsheetApp.getUi().showModalDialog(html, 'Открываем Финпространство');
}
