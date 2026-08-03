const FINSPACE = Object.freeze({
  VERSION: 1,
  TEMPLATE_VERSION: 1,
  API_ROOT: '/api/v1/google-sheets/apps-script',
  BINDING_KEY: 'FINSPACE_BINDING_ID',
  SECRET_KEY: 'FINSPACE_BINDING_SECRET',
  BACKEND_KEY: 'FINSPACE_BACKEND_URL',
  APP_KEY: 'FINSPACE_APP_URL',
  QUEUE_KEY: 'FINSPACE_EDIT_QUEUE_V1',
  SYNC_GUARD_KEY: 'FINSPACE_SYNC_GUARD',
  INTERVAL_KEY: 'FINSPACE_SYNC_INTERVAL_MINUTES',
  RECONCILE_REMINDER_KEY: 'FINSPACE_RECONCILE_REMINDER',
  LAST_ERROR_KEY: 'FINSPACE_LAST_ERROR',
  PUSH_BATCH_SIZE: 100,
  PULL_BATCH_SIZE: 100,
  RECONCILE_BATCH_SIZE: 500,
  MAX_QUEUE_SIZE: 500,
  ALLOWED_INTERVALS: [1, 5, 10, 15, 30]
});

function finspaceProperties_() {
  return PropertiesService.getDocumentProperties();
}

function normalizeBackendUrl_(value) {
  let url = String(value || '').trim().replace(/\/+$/, '');
  url = url.replace(/\/api\/v1\/google-sheets\/apps-script$/i, '');
  if (!/^https:\/\//i.test(url)) {
    throw new Error('Backend URL должен быть публичным HTTPS URL.');
  }
  return url;
}

function finspaceConfig_() {
  const properties = finspaceProperties_();
  const config = {
    backendUrl: properties.getProperty(FINSPACE.BACKEND_KEY),
    bindingId: properties.getProperty(FINSPACE.BINDING_KEY),
    secret: properties.getProperty(FINSPACE.SECRET_KEY),
    appUrl: properties.getProperty(FINSPACE.APP_KEY) || 'http://localhost:3000',
    intervalMinutes: Number(properties.getProperty(FINSPACE.INTERVAL_KEY) || 5)
  };
  if (!config.backendUrl || !config.bindingId || !config.secret) {
    throw new Error('Подключение не настроено. Запустите «Настроить подключение».');
  }
  return config;
}

function configureConnection() {
  const ui = SpreadsheetApp.getUi();
  const backend = ui.prompt(
    'Финпространство',
    'Публичный HTTPS URL backend (например, https://example.trycloudflare.com):',
    ui.ButtonSet.OK_CANCEL
  );
  if (backend.getSelectedButton() !== ui.Button.OK) return;
  const binding = ui.prompt(
    'Финпространство',
    'Binding ID из приложения:',
    ui.ButtonSet.OK_CANCEL
  );
  if (binding.getSelectedButton() !== ui.Button.OK) return;
  const secret = ui.prompt(
    'Финпространство',
    'Одноразово показанный binding secret:',
    ui.ButtonSet.OK_CANCEL
  );
  if (secret.getSelectedButton() !== ui.Button.OK) return;
  const interval = ui.prompt(
    'Финпространство',
    'Интервал синхронизации в минутах: 1, 5, 10, 15 или 30',
    ui.ButtonSet.OK_CANCEL
  );
  if (interval.getSelectedButton() !== ui.Button.OK) return;

  const intervalMinutes = Number(interval.getResponseText().trim());
  if (FINSPACE.ALLOWED_INTERVALS.indexOf(intervalMinutes) < 0) {
    throw new Error('Недопустимый интервал. Используйте 1, 5, 10, 15 или 30 минут.');
  }
  const bindingId = binding.getResponseText().trim();
  const bindingSecret = secret.getResponseText().trim();
  if (!bindingId || !bindingSecret) throw new Error('Binding ID и secret обязательны.');

  finspaceProperties_().setProperties({
    [FINSPACE.BACKEND_KEY]: normalizeBackendUrl_(backend.getResponseText()),
    [FINSPACE.BINDING_KEY]: bindingId,
    [FINSPACE.SECRET_KEY]: bindingSecret,
    [FINSPACE.INTERVAL_KEY]: String(intervalMinutes)
  }, false);
  const registration = registerBinding();
  heartbeat_();
  ui.alert(
    'Подключение сохранено в Document Properties. Регистрация: ' + registration.status +
    '. Secret не записан в ячейки книги.'
  );
}

function updateBackendUrl() {
  const ui = SpreadsheetApp.getUi();
  const properties = finspaceProperties_();
  const previousUrl = properties.getProperty(FINSPACE.BACKEND_KEY);
  if (!properties.getProperty(FINSPACE.BINDING_KEY) ||
      !properties.getProperty(FINSPACE.SECRET_KEY)) {
    throw new Error('Подключение не настроено. Сначала запустите «Настроить подключение».');
  }
  const answer = ui.prompt(
    'Обновить URL backend',
    'Новый публичный HTTPS URL backend:',
    ui.ButtonSet.OK_CANCEL
  );
  if (answer.getSelectedButton() !== ui.Button.OK) return;
  const nextUrl = normalizeBackendUrl_(answer.getResponseText());
  properties.setProperty(FINSPACE.BACKEND_KEY, nextUrl);
  try {
    heartbeat_();
  } catch (error) {
    if (previousUrl) {
      properties.setProperty(FINSPACE.BACKEND_KEY, previousUrl);
    } else {
      properties.deleteProperty(FINSPACE.BACKEND_KEY);
    }
    throw error;
  }
  ui.alert('URL backend обновлён. Binding ID и secret не изменялись.');
}

function registerBinding() {
  const spreadsheet = SpreadsheetApp.getActive();
  const result = signedRequest_('/register', {
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_url: spreadsheet.getUrl(),
    template_version: FINSPACE.TEMPLATE_VERSION,
    apps_script_version: FINSPACE.VERSION
  });
  finspaceProperties_().deleteProperty(FINSPACE.LAST_ERROR_KEY);
  return result;
}

function updateRotatedSecret() {
  const ui = SpreadsheetApp.getUi();
  const answer = ui.prompt(
    'Повернуть секрет',
    'Сначала поверните secret в приложении, затем вставьте новое значение сюда:',
    ui.ButtonSet.OK_CANCEL
  );
  if (answer.getSelectedButton() !== ui.Button.OK) return;
  const value = answer.getResponseText().trim();
  if (!value) throw new Error('Secret не может быть пустым.');
  finspaceProperties_().setProperty(FINSPACE.SECRET_KEY, value);
  heartbeat_();
  ui.alert('Новый secret проверен и сохранён.');
}

function resetLocalConfiguration() {
  const ui = SpreadsheetApp.getUi();
  if (ui.alert(
    'Сбросить локальную конфигурацию?',
    'Binding на backend не удаляется. Локальные secret, URL и очередь будут очищены.',
    ui.ButtonSet.YES_NO
  ) !== ui.Button.YES) return;
  removeTriggers();
  const properties = finspaceProperties_();
  [
    FINSPACE.BINDING_KEY,
    FINSPACE.SECRET_KEY,
    FINSPACE.BACKEND_KEY,
    FINSPACE.APP_KEY,
    FINSPACE.QUEUE_KEY,
    FINSPACE.SYNC_GUARD_KEY,
    FINSPACE.INTERVAL_KEY,
    FINSPACE.RECONCILE_REMINDER_KEY,
    FINSPACE.LAST_ERROR_KEY
  ].forEach(function(key) { properties.deleteProperty(key); });
  ui.alert('Локальная конфигурация и очередь очищены.');
}

function bytesToHex_(bytes) {
  return bytes.map(function(value) {
    const normalized = value < 0 ? value + 256 : value;
    return ('0' + normalized.toString(16)).slice(-2);
  }).join('');
}
