function captureFinspaceEdit(event) {
  if (!event || !event.range) return;
  if (finspaceProperties_().getProperty(FINSPACE.SYNC_GUARD_KEY) === '1') return;
  const sheet = event.range.getSheet();
  const layout = sheetLayout_(sheet.getName());
  if (!layout || event.range.getLastRow() < 2) return;

  const firstColumn = event.range.getColumn();
  const lastColumn = event.range.getLastColumn();
  if (lastColumn >= layout.technicalStart) {
    for (let row = Math.max(2, event.range.getRow()); row <= event.range.getLastRow(); row += 1) {
      sheet.getRange(row, layout.statusColumn).setValue('TAMPER');
      sheet.getRange(row, layout.errorColumn).setValue(
        'Изменён защищённый технический столбец. Выполните полную сверку.'
      );
    }
    return;
  }
  const touchesEditable = layout.editableColumns.some(function(column) {
    return column >= firstColumn && column <= lastColumn;
  });
  if (!touchesEditable) return;
  for (let row = Math.max(2, event.range.getRow()); row <= event.range.getLastRow(); row += 1) {
    sheet.getRange(row, layout.statusColumn).setValue('DIRTY');
    sheet.getRange(row, layout.errorColumn).clearContent();
    enqueueFinspaceEdit_(sheet.getName(), row);
  }
}

function enqueueFinspaceEdit_(sheetName, rowNumber) {
  const properties = finspaceProperties_();
  const queue = queuedFinspaceEdits_();
  const key = sheetName + ':' + rowNumber;
  const existing = queue.filter(function(item) { return item.key === key; })[0];
  const filtered = queue.filter(function(item) { return item.key !== key; });
  filtered.push({
    key: key,
    eventId: existing ? existing.eventId : Utilities.getUuid(),
    sheetName: sheetName,
    rowNumber: rowNumber,
    queuedAt: new Date().toISOString()
  });
  properties.setProperty(
    FINSPACE.QUEUE_KEY,
    JSON.stringify(filtered.slice(-FINSPACE.MAX_QUEUE_SIZE))
  );
}

function queuedFinspaceEdits_() {
  const raw = finspaceProperties_().getProperty(FINSPACE.QUEUE_KEY) || '[]';
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    finspaceProperties_().setProperty(FINSPACE.LAST_ERROR_KEY, 'Повреждена локальная очередь.');
    return [];
  }
}

function replaceFinspaceQueue_(queue) {
  finspaceProperties_().setProperty(FINSPACE.QUEUE_KEY, JSON.stringify(queue));
}

function removeQueuedFinspaceEdits_(keys) {
  const keySet = {};
  keys.forEach(function(key) { keySet[key] = true; });
  replaceFinspaceQueue_(queuedFinspaceEdits_().filter(function(item) {
    return !keySet[item.key];
  }));
}

function sheetLayout_(sheetName) {
  return {
    'Операции': {
      entityType: 'transaction', idColumn: 19, versionColumn: 24,
      hashColumn: 25, deletedColumn: 27, statusColumn: 17, errorColumn: 18,
      technicalStart: 19, width: 27,
      editableColumns: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    },
    'Счета': {
      entityType: 'account', idColumn: 13, versionColumn: 14,
      hashColumn: 15, deletedColumn: 17, statusColumn: 11, errorColumn: 12,
      technicalStart: 13, width: 17,
      editableColumns: [1, 2, 4, 8]
    },
    'Категории': {
      entityType: 'category', idColumn: 11, versionColumn: 13,
      hashColumn: 14, deletedColumn: 16, statusColumn: 9, errorColumn: 10,
      technicalStart: 11, width: 16,
      editableColumns: [1, 2, 3, 4, 5, 6, 7]
    }
  }[sheetName] || null;
}
