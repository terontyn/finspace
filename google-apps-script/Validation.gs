function payloadForEdit_(item) {
  const spreadsheet = SpreadsheetApp.getActive();
  const sheet = spreadsheet.getSheetByName(item.sheetName);
  const layout = sheetLayout_(item.sheetName);
  if (!sheet || !layout || item.rowNumber < 2 || item.rowNumber > sheet.getMaxRows()) {
    throw new Error('Строка локальной очереди больше не существует.');
  }
  const raw = sheet.getRange(item.rowNumber, 1, 1, layout.width).getValues()[0];
  const display = sheet.getRange(item.rowNumber, 1, 1, layout.width).getDisplayValues()[0];
  if (!display.some(function(value) { return value !== ''; })) {
    throw new Error('Пустая строка не может быть отправлена.');
  }
  let changedFields;
  let visibleRow;
  if (layout.entityType === 'transaction') {
    const date = formatSheetDate_(raw[0], display[0], 'yyyy-MM-dd');
    const time = formatSheetDate_(raw[1], display[1], 'HH:mm:ss') || '00:00:00';
    changedFields = {
      date: date,
      time: time,
      transaction_type: display[2],
      amount: display[3],
      currency: display[4],
      account: display[5],
      target_account: display[6],
      category: display[7],
      counterparty: display[9],
      description: display[10],
      comment: display[11],
      status: display[12]
    };
    visibleRow = Object.assign({}, changedFields, {
      _account_id: display[20],
      _target_account_id: display[21],
      _category_id: display[22]
    });
  } else if (layout.entityType === 'account') {
    changedFields = {
      name: display[0],
      account_type: display[1],
      institution: display[3],
      is_archived: display[7]
    };
    visibleRow = Object.assign({}, changedFields);
  } else {
    changedFields = {
      name: display[0],
      category_type: display[1],
      parent: display[2],
      icon: display[3],
      color: display[4],
      sort_order: display[5],
      is_archived: display[6]
    };
    visibleRow = Object.assign({}, changedFields, { _parent_id: display[11] });
  }
  return {
    event_id: item.eventId,
    spreadsheet_id: spreadsheet.getId(),
    sheet_name: item.sheetName,
    row_number: item.rowNumber,
    entity_type: layout.entityType,
    entity_id: display[layout.idColumn - 1] || null,
    expected_version: display[layout.versionColumn - 1]
      ? Number(display[layout.versionColumn - 1]) : null,
    row_hash: display[layout.hashColumn - 1] || null,
    changed_fields: changedFields,
    visible_row: visibleRow
  };
}

function formatSheetDate_(raw, display, pattern) {
  if (Object.prototype.toString.call(raw) === '[object Date]' && !isNaN(raw.getTime())) {
    return Utilities.formatDate(raw, SpreadsheetApp.getActive().getSpreadsheetTimeZone(), pattern);
  }
  const value = String(display || '').trim();
  const russian = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(value);
  if (pattern === 'yyyy-MM-dd' && russian) return russian[3] + '-' + russian[2] + '-' + russian[1];
  return value;
}

function applyNormalizedRow_(sheet, rowNumber, normalized) {
  if (!normalized) return;
  const indexes = Object.keys(normalized).map(Number);
  if (!indexes.length) return;
  const width = Math.max.apply(null, indexes) + 1;
  const values = sheet.getRange(rowNumber, 1, 1, width).getValues()[0];
  indexes.forEach(function(index) { values[index] = normalized[String(index)]; });
  withSyncGuard_(function() {
    sheet.getRange(rowNumber, 1, 1, width).setValues([values]);
  });
}

function applyNormalizedTechnicalRow_(sheet, rowNumber, normalized) {
  if (!normalized) return;
  const layout = sheetLayout_(sheet.getName());
  if (!layout) return;
  const values = sheet.getRange(rowNumber, 1, 1, layout.width).getValues()[0];
  Object.keys(normalized).map(Number).forEach(function(index) {
    if (index + 1 >= layout.technicalStart && index < values.length) {
      values[index] = normalized[String(index)];
    }
  });
  withSyncGuard_(function() {
    sheet.getRange(rowNumber, 1, 1, layout.width).setValues([values]);
  });
}

function withSyncGuard_(callback) {
  const properties = finspaceProperties_();
  properties.setProperty(FINSPACE.SYNC_GUARD_KEY, '1');
  try {
    callback();
  } finally {
    properties.deleteProperty(FINSPACE.SYNC_GUARD_KEY);
  }
}

function validateCurrentRow() {
  const range = SpreadsheetApp.getActiveRange();
  if (!range || !sheetLayout_(range.getSheet().getName()) || range.getRow() < 2) {
    SpreadsheetApp.getUi().alert('Выберите строку Операций, Счетов или Категорий.');
    return;
  }
  const item = {
    eventId: Utilities.getUuid(),
    sheetName: range.getSheet().getName(),
    rowNumber: range.getRow()
  };
  try {
    const payload = payloadForEdit_(item);
    if (payload.entity_type === 'transaction' && !payload.entity_id) {
      const required = ['date', 'transaction_type', 'amount', 'currency', 'account'];
      const missing = required.filter(function(field) {
        return !payload.changed_fields[field];
      });
      if (missing.length) throw new Error('Не заполнено: ' + missing.join(', '));
    }
    SpreadsheetApp.getUi().alert('Базовая проверка пройдена.');
  } catch (error) {
    SpreadsheetApp.getUi().alert(String(error));
  }
}
