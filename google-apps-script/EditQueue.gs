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
    try {
      enqueueFinspaceEdit_(sheet.getName(), row);
    } catch (error) {
      sheet.getRange(row, layout.errorColumn).setValue(
        ('Локальная очередь не сохранена: ' + safeFinspaceErrorMessage_(error)).slice(0, 500)
      );
      throw error;
    }
  }
}

function enqueueFinspaceEdit_(sheetName, rowNumber) {
  return withFinspaceQueueLock_(function() {
    const queue = readFinspaceQueueOrRecoverUnsafe_();
    const key = queueKey_(sheetName, rowNumber);
    const filtered = queue.filter(function(item) { return item.key !== key; });
    if (filtered.length >= FINSPACE.MAX_QUEUE_SIZE) {
      throw finspaceQueueError_(
        'LOCAL_QUEUE_FULL',
        'Локальная очередь заполнена. Выполните отправку или сверку.'
      );
    }
    // Every real user edit receives a new business event identity. A retry keeps
    // the existing item instead and therefore keeps its eventId.
    const item = {
      key: key,
      eventId: Utilities.getUuid(),
      sheetName: sheetName,
      rowNumber: rowNumber,
      queuedAt: new Date().toISOString(),
      attemptCount: 0,
      lastAttemptAt: null
    };
    writeQueueEventNote_(sheetName, rowNumber, item.eventId);
    filtered.push(item);
    writeFinspaceQueueUnsafe_(filtered);
    return item;
  });
}

function queuedFinspaceEdits_() {
  return withFinspaceQueueLock_(function() {
    return readFinspaceQueueOrRecoverUnsafe_().map(function(item) {
      return Object.assign({}, item);
    });
  });
}

function readFinspaceQueueUnsafe_() {
  const raw = finspaceProperties_().getProperty(FINSPACE.QUEUE_KEY) || '[]';
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error('Queue root must be an array');
    const valid = parsed.filter(isValidFinspaceQueueItem_);
    if (valid.length !== parsed.length) {
      finspaceProperties_().setProperty(
        FINSPACE.LAST_ERROR_KEY,
        'Локальная очередь содержала повреждённые элементы; они изолированы.'
      );
    }
    return valid;
  } catch (error) {
    throw finspaceQueueError_('LOCAL_QUEUE_CORRUPTED', 'Повреждена локальная очередь.');
  }
}

function replaceFinspaceQueue_(queue) {
  return withFinspaceQueueLock_(function() {
    writeFinspaceQueueUnsafe_(queue);
  });
}

function writeFinspaceQueueUnsafe_(queue) {
  if (!Array.isArray(queue)) {
    throw finspaceQueueError_('LOCAL_QUEUE_INVALID', 'Локальная очередь должна быть массивом.');
  }
  if (queue.length > FINSPACE.MAX_QUEUE_SIZE) {
    throw finspaceQueueError_(
      'LOCAL_QUEUE_FULL',
      'Локальная очередь заполнена. События не были отброшены.'
    );
  }
  finspaceProperties_().setProperty(FINSPACE.QUEUE_KEY, JSON.stringify(queue));
}

function readFinspaceQueueOrRecoverUnsafe_() {
  try {
    return readFinspaceQueueUnsafe_();
  } catch (error) {
    if (error.code !== 'LOCAL_QUEUE_CORRUPTED') throw error;
    const properties = finspaceProperties_();
    const raw = properties.getProperty(FINSPACE.QUEUE_KEY) || '';
    properties.setProperty(FINSPACE.QUEUE_CORRUPT_KEY, String(raw).slice(0, 8000));
    properties.setProperty(
      FINSPACE.LAST_ERROR_KEY,
      'Повреждена локальная очередь; выполнено восстановление по маркерам строк.'
    );
    properties.setProperty(FINSPACE.QUEUE_KEY, '[]');
    return [];
  }
}

function recoverPendingFinspaceEdits_() {
  return withFinspaceQueueLock_(function() {
    const queue = readFinspaceQueueOrRecoverUnsafe_();
    const byKey = {};
    queue.forEach(function(item) { byKey[item.key] = item; });
    let recovered = 0;
    ['Операции', 'Счета', 'Категории'].forEach(function(sheetName) {
      const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
      const layout = sheetLayout_(sheetName);
      if (!sheet || !layout || sheet.getLastRow() < 2) return;
      const statuses = sheet.getRange(
        2,
        layout.statusColumn,
        sheet.getLastRow() - 1,
        1
      ).getDisplayValues();
      statuses.forEach(function(values, index) {
        const status = String(values[0] || '').trim().toUpperCase();
        if (status !== 'DIRTY' && status !== 'PENDING') return;
        const rowNumber = index + 2;
        const key = queueKey_(sheetName, rowNumber);
        if (byKey[key]) {
          writeQueueEventNote_(sheetName, rowNumber, byKey[key].eventId);
          return;
        }
        let eventId = queueEventIdFromRow_(sheetName, rowNumber);
        if (!eventId && status === 'PENDING') {
          const entityId = sheet.getRange(rowNumber, layout.idColumn).getDisplayValue();
          if (!entityId) {
            sheet.getRange(rowNumber, layout.statusColumn).setValue('ERROR');
            sheet.getRange(rowNumber, layout.errorColumn).setValue(
              'PENDING без event ID нельзя безопасно повторить. Выполните полную сверку.'
            );
            return;
          }
        }
        eventId = eventId || Utilities.getUuid();
        if (queue.length >= FINSPACE.MAX_QUEUE_SIZE) return;
        const item = {
          key: key,
          eventId: eventId,
          sheetName: sheetName,
          rowNumber: rowNumber,
          queuedAt: new Date().toISOString(),
          attemptCount: 0,
          lastAttemptAt: null
        };
        writeQueueEventNote_(sheetName, rowNumber, eventId);
        queue.push(item);
        byKey[key] = item;
        recovered += 1;
      });
    });
    writeFinspaceQueueUnsafe_(queue);
    return recovered;
  });
}

function withFinspaceQueueLock_(callback) {
  const lock = LockService.getDocumentLock();
  lock.waitLock(FINSPACE.QUEUE_LOCK_TIMEOUT_MS);
  try {
    return callback();
  } finally {
    lock.releaseLock();
  }
}

function queueKey_(sheetName, rowNumber) {
  return sheetName + ':' + rowNumber;
}

function isValidFinspaceQueueItem_(item) {
  return Boolean(
    item &&
    typeof item.key === 'string' && item.key &&
    typeof item.eventId === 'string' && item.eventId &&
    typeof item.sheetName === 'string' && sheetLayout_(item.sheetName) &&
    Number.isInteger(Number(item.rowNumber)) && Number(item.rowNumber) >= 2
  );
}

function queueEventIdFromRow_(sheetName, rowNumber) {
  const layout = sheetLayout_(sheetName);
  const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
  if (!layout || !sheet) return '';
  const note = sheet.getRange(rowNumber, layout.statusColumn).getNote() || '';
  const lines = String(note).split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index].indexOf(FINSPACE.QUEUE_NOTE_PREFIX) === 0) {
      return lines[index].slice(FINSPACE.QUEUE_NOTE_PREFIX.length).trim();
    }
  }
  return '';
}

function writeQueueEventNote_(sheetName, rowNumber, eventId) {
  const layout = sheetLayout_(sheetName);
  const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
  if (!layout || !sheet) return;
  const range = sheet.getRange(rowNumber, layout.statusColumn);
  const retained = String(range.getNote() || '').split(/\r?\n/).filter(function(line) {
    return line && line.indexOf(FINSPACE.QUEUE_NOTE_PREFIX) !== 0;
  });
  retained.push(FINSPACE.QUEUE_NOTE_PREFIX + eventId);
  range.setNote(retained.join('\n'));
}

function clearQueueEventNote_(sheetName, rowNumber, eventId) {
  const layout = sheetLayout_(sheetName);
  const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
  if (!layout || !sheet) return;
  const range = sheet.getRange(rowNumber, layout.statusColumn);
  const marker = FINSPACE.QUEUE_NOTE_PREFIX + eventId;
  const retained = String(range.getNote() || '').split(/\r?\n/).filter(function(line) {
    return line && line !== marker;
  });
  range.setNote(retained.join('\n'));
}

function finspaceQueueError_(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function safeFinspaceErrorMessage_(error) {
  return error && error.message ? String(error.message) : 'Неизвестная ошибка';
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
