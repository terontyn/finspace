const FINSPACE_TEMPLATE = Object.freeze({
  SHEETS: [
    'Сегодня', 'Операции', 'Счета', 'Категории', 'Бюджет', 'Цели', 'Долги',
    'Импорт', 'Ошибки', 'Конфликты', 'Инструкция', '_sync_meta', '_lists'
  ],
  HEADERS: {
    'Операции': [
      'Дата', 'Время', 'Тип', 'Сумма', 'Валюта', 'Счёт', 'Счёт назначения',
      'Категория', 'Подкатегория', 'Контрагент', 'Описание', 'Комментарий',
      'Статус', 'Источник', 'Владелец', 'Последнее изменение', 'Синхронизация',
      'Ошибка', '_id', '_workspace_id', '_account_id', '_target_account_id',
      '_category_id', '_version', '_row_hash', '_updated_at', '_deleted_at'
    ],
    'Счета': [
      'Название', 'Тип', 'Валюта', 'Учреждение', 'Начальный остаток',
      'Дата начального остатка', 'Кредитный лимит', 'Архив',
      'Рассчитанный остаток', 'Последнее изменение', 'Синхронизация', 'Ошибка',
      '_id', '_version', '_row_hash', '_updated_at', '_deleted_at'
    ],
    'Категории': [
      'Название', 'Тип', 'Родитель', 'Иконка', 'Цвет', 'Порядок', 'Архив',
      'Последнее изменение', 'Синхронизация', 'Ошибка', '_id', '_parent_id',
      '_version', '_row_hash', '_updated_at', '_deleted_at'
    ],
    'Ошибки': [
      'Дата', 'Лист', 'Строка', 'Тип объекта', 'Ошибка', 'Код',
      'Рекомендуемое действие', 'Повторить', '_event_id'
    ],
    'Конфликты': [
      'Тип', 'Объект', 'Дата конфликта', 'Поля', 'Значение PostgreSQL',
      'Значение Google Sheets', 'Версия PostgreSQL', 'Версия Google Sheets',
      'Статус', 'Решение', 'Ссылка в приложение', '_conflict_id'
    ]
  },
  TECHNICAL: {
    'Операции': { start: 19, count: 9 },
    'Счета': { start: 13, count: 5 },
    'Категории': { start: 11, count: 6 }
  }
});

function setupFinspace() {
  const lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    const spreadsheet = SpreadsheetApp.getActive();
    spreadsheet.setSpreadsheetLocale('ru_RU');
    spreadsheet.setSpreadsheetTimeZone('Asia/Yekaterinburg');
    FINSPACE_TEMPLATE.SHEETS.forEach(function(name) {
      ensureFinspaceSheet_(spreadsheet, name);
    });
    initializeTemplateValues_(spreadsheet);
    formatTemplate_(spreadsheet);
    defineNamedRanges_(spreadsheet);
    installValidations_(spreadsheet);
    refreshReferenceLists_();
    spreadsheet.getSheetByName('_sync_meta').hideSheet();
    spreadsheet.getSheetByName('_lists').hideSheet();
    spreadsheet.toast('Шаблон v1 установлен.', 'Финпространство');
  } finally {
    lock.releaseLock();
  }
}

function ensureFinspaceSheet_(spreadsheet, name) {
  let sheet = spreadsheet.getSheetByName(name);
  if (!sheet) sheet = spreadsheet.insertSheet(name);
  const headers = FINSPACE_TEMPLATE.HEADERS[name];
  if (headers) {
    if (sheet.getMaxColumns() < headers.length) {
      sheet.insertColumnsAfter(sheet.getMaxColumns(), headers.length - sheet.getMaxColumns());
    }
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  }
  return sheet;
}

function initializeTemplateValues_(spreadsheet) {
  spreadsheet.getSheetByName('Сегодня').getRange('A1:B6').setValues([
    ['Финпространство', 'Сводка только для чтения'],
    ['Источник истины', 'PostgreSQL'],
    ['Операции', 'См. лист «Операции»'],
    ['Счета', 'См. лист «Счета»'],
    ['Категории', 'См. лист «Категории»'],
    ['Синхронизация', 'Apps Script Bridge']
  ]);
  spreadsheet.getSheetByName('Бюджет').getRange('A1').setValue(
    'Модуль бюджета будет подключён на следующем этапе.'
  );
  spreadsheet.getSheetByName('Цели').getRange('A1').setValue(
    'Модуль целей будет подключён позже.'
  );
  spreadsheet.getSheetByName('Долги').getRange('A1').setValue(
    'Модуль долгов будет подключён позже.'
  );
  spreadsheet.getSheetByName('Импорт').getRange('A1').setValue(
    'Импорт выполняется через проверяемый staging-процесс приложения.'
  );
  spreadsheet.getSheetByName('Инструкция').getRange('A1:A9').setValues([
    ['Финпространство — Apps Script Bridge'],
    ['PostgreSQL является источником финансовой истины.'],
    ['Google Cloud OAuth и Google Cloud project не требуются.'],
    ['Не редактируйте скрытые технические столбцы.'],
    ['Редактирование строки ставит её в локальную очередь DIRTY.'],
    ['Плановый триггер отправляет изменения пакетами и получает обновления.'],
    ['Физическое удаление строки не удаляет данные приложения.'],
    ['Конфликты разрешаются в приложении.'],
    ['Secret хранится только в Document Properties.']
  ]);
  spreadsheet.getSheetByName('_sync_meta').getRange('A1:B7').setValues([
    ['template_version', String(FINSPACE.TEMPLATE_VERSION)],
    ['apps_script_version', String(FINSPACE.VERSION)],
    ['spreadsheet_id', spreadsheet.getId()],
    ['binding_id', finspaceProperties_().getProperty(FINSPACE.BINDING_KEY) || ''],
    ['provider', 'apps_script_bridge'],
    ['last_pull_at', ''],
    ['last_reconciliation_at', '']
  ]);
  spreadsheet.getSheetByName('_lists').getRange('A1:J6').setValues([
    ['account_names', 'account_ids', 'category_names', 'category_ids',
      'transaction_types', 'transaction_statuses', 'currency_codes',
      'account_types', 'template_versions', 'workspace_settings'],
    ['', '', '', '', 'Доход', 'Черновик', 'RUB', 'cash', '1', ''],
    ['', '', '', '', 'Расход', 'Подтверждена', 'USD', 'debit_card', '', ''],
    ['', '', '', '', 'Перевод', 'Сверена', 'EUR', 'credit_card', '', ''],
    ['', '', '', '', 'Возврат', 'Отменена', '', 'savings', '', ''],
    ['', '', '', '', 'Корректировка', '', '', 'other', '', '']
  ]);
}

function formatTemplate_(spreadsheet) {
  Object.keys(FINSPACE_TEMPLATE.HEADERS).forEach(function(name) {
    const sheet = spreadsheet.getSheetByName(name);
    const width = FINSPACE_TEMPLATE.HEADERS[name].length;
    sheet.setFrozenRows(1);
    const header = sheet.getRange(1, 1, 1, width);
    header.setBackground('#1f3b5b').setFontColor('#ffffff').setFontWeight('bold')
      .setWrap(true);
    if (!sheet.getFilter()) sheet.getRange(1, 1, sheet.getMaxRows(), width).createFilter();
    sheet.autoResizeColumns(1, width);
  });
  spreadsheet.getSheetByName('Операции').getRange('A2:A').setNumberFormat('dd.MM.yyyy');
  spreadsheet.getSheetByName('Операции').getRange('B2:B').setNumberFormat('HH:mm:ss');
  spreadsheet.getSheetByName('Операции').getRange('D2:D').setNumberFormat('#,##0.00');
  spreadsheet.getSheetByName('Счета').getRange('E2:I').setNumberFormat('#,##0.00');
  Object.keys(FINSPACE_TEMPLATE.TECHNICAL).forEach(function(name) {
    const sheet = spreadsheet.getSheetByName(name);
    const technical = FINSPACE_TEMPLATE.TECHNICAL[name];
    sheet.hideColumns(technical.start, technical.count);
    replaceProtection_(sheet.getRange(1, technical.start, sheet.getMaxRows(), technical.count),
      'Технические поля Финпространства');
  });
  ['Сегодня', 'Ошибки', 'Конфликты', '_sync_meta'].forEach(function(name) {
    replaceProtection_(spreadsheet.getSheetByName(name).getDataRange(),
      'Диапазон управляется Финпространством');
  });
  applySyncConditionalFormatting_(spreadsheet.getSheetByName('Операции'), 17);
  applySyncConditionalFormatting_(spreadsheet.getSheetByName('Счета'), 11);
  applySyncConditionalFormatting_(spreadsheet.getSheetByName('Категории'), 9);
}

function replaceProtection_(range, description) {
  range.getSheet().getProtections(SpreadsheetApp.ProtectionType.RANGE)
    .filter(function(item) { return item.getDescription() === description; })
    .forEach(function(item) { item.remove(); });
  range.protect().setDescription(description).setWarningOnly(true);
}

function applySyncConditionalFormatting_(sheet, column) {
  const range = sheet.getRange(2, column, sheet.getMaxRows() - 1, 1);
  const retained = sheet.getConditionalFormatRules().filter(function(rule) {
    return rule.getRanges().every(function(item) { return item.getColumn() !== column; });
  });
  const colors = {
    SYNCED: '#c6efce', DIRTY: '#ffeb9c', PENDING: '#ffeb9c',
    CONFLICT: '#f4b183', ERROR: '#ffc7ce', DELETED: '#dddddd', TAMPER: '#ffc7ce'
  };
  Object.keys(colors).forEach(function(status) {
    retained.push(SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo(status)
      .setBackground(colors[status])
      .setRanges([range])
      .build());
  });
  sheet.setConditionalFormatRules(retained);
}

function defineNamedRanges_(spreadsheet) {
  const lists = spreadsheet.getSheetByName('_lists');
  const definitions = {
    account_names: 1, account_ids: 2, category_names: 3, category_ids: 4,
    transaction_types: 5, transaction_statuses: 6, currency_codes: 7,
    account_types: 8
  };
  Object.keys(definitions).forEach(function(name) {
    spreadsheet.setNamedRange(name, lists.getRange(2, definitions[name], 999, 1));
  });
}

function installValidations_(spreadsheet) {
  const operations = spreadsheet.getSheetByName('Операции');
  const validations = [
    [3, 'transaction_types'], [5, 'currency_codes'], [6, 'account_names'],
    [7, 'account_names'], [8, 'category_names'], [13, 'transaction_statuses']
  ];
  validations.forEach(function(item) {
    operations.getRange(2, item[0], operations.getMaxRows() - 1, 1)
      .setDataValidation(SpreadsheetApp.newDataValidation()
        .requireValueInRange(spreadsheet.getRangeByName(item[1]), true)
        .setAllowInvalid(false)
        .build());
  });
}

function refreshReferenceLists_() {
  const spreadsheet = SpreadsheetApp.getActive();
  const lists = spreadsheet.getSheetByName('_lists');
  if (!lists) return;
  const accounts = spreadsheet.getSheetByName('Счета');
  const categories = spreadsheet.getSheetByName('Категории');
  const accountRows = referenceRows_(accounts, 1, 13, 17);
  const categoryRows = referenceRows_(categories, 1, 11, 16);
  lists.getRange(2, 1, Math.max(lists.getMaxRows() - 1, 1), 4).clearContent();
  const count = Math.max(accountRows.length, categoryRows.length);
  if (!count) return;
  const values = [];
  for (let index = 0; index < count; index += 1) {
    values.push([
      accountRows[index] ? accountRows[index][0] : '',
      accountRows[index] ? accountRows[index][1] : '',
      categoryRows[index] ? categoryRows[index][0] : '',
      categoryRows[index] ? categoryRows[index][1] : ''
    ]);
  }
  lists.getRange(2, 1, values.length, 4).setValues(values);
}

function referenceRows_(sheet, nameColumn, idColumn, deletedColumn) {
  if (!sheet || sheet.getLastRow() < 2) return [];
  return sheet.getRange(2, 1, sheet.getLastRow() - 1, sheet.getLastColumn())
    .getDisplayValues()
    .filter(function(row) { return row[idColumn - 1] && !row[deletedColumn - 1]; })
    .map(function(row) { return [row[nameColumn - 1], row[idColumn - 1]]; });
}
