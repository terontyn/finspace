function signedRequest_(path, payload) {
  const config = finspaceConfig_();
  const body = JSON.stringify(payload || {});
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const nonce = Utilities.getUuid();
  const bodyHash = bytesToHex_(Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256,
    body,
    Utilities.Charset.UTF_8
  ));
  const signingKey = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256,
    config.secret,
    Utilities.Charset.UTF_8
  );
  const message = timestamp + '\n' + nonce + '\n' + bodyHash;
  const messageBytes = Utilities.newBlob(message).getBytes();
  const signature = bytesToHex_(Utilities.computeHmacSha256Signature(
    messageBytes,
    signingKey
  ));
  const response = UrlFetchApp.fetch(
    config.backendUrl + FINSPACE.API_ROOT + path,
    {
      method: 'post',
      contentType: 'application/json',
      payload: body,
      muteHttpExceptions: true,
      headers: {
        'X-Finspace-Binding-ID': config.bindingId,
        'X-Finspace-Timestamp': timestamp,
        'X-Finspace-Nonce': nonce,
        'X-Finspace-Body-SHA256': bodyHash,
        'X-Finspace-Signature': signature
      }
    }
  );
  let parsed = {};
  try {
    parsed = JSON.parse(response.getContentText() || '{}');
  } catch (error) {
    throw new Error('Backend вернул ответ не в JSON. HTTP ' + response.getResponseCode());
  }
  if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
    const apiError = parsed.error || {};
    throw new Error(
      (apiError.code || 'BRIDGE_HTTP_ERROR') + ': ' +
      (apiError.message || ('HTTP ' + response.getResponseCode()))
    );
  }
  return parsed;
}

function pushPendingChanges() {
  return pushPendingChanges_(true);
}

function pushPendingChanges_(notify) {
  const queue = queuedFinspaceEdits_().slice(0, FINSPACE.PUSH_BATCH_SIZE);
  if (!queue.length) {
    if (notify) SpreadsheetApp.getActive().toast('Локальная очередь пуста.', 'Финпространство');
    return { sent: 0, remaining: 0 };
  }
  const validItems = [];
  const payloads = [];
  queue.forEach(function(item) {
    try {
      const payload = payloadForEdit_(item);
      const layout = sheetLayout_(item.sheetName);
      const sheet = SpreadsheetApp.getActive().getSheetByName(item.sheetName);
      sheet.getRange(item.rowNumber, layout.statusColumn).setValue('PENDING');
      payloads.push(payload);
      validItems.push(item);
    } catch (error) {
      recordSheetError_(item, 'LOCAL_VALIDATION_ERROR', String(error));
    }
  });
  if (!payloads.length) return { sent: 0, remaining: queuedFinspaceEdits_().length };

  const response = signedRequest_('/push', { events: payloads });
  const itemByEvent = {};
  validItems.forEach(function(item) { itemByEvent[item.eventId] = item; });
  const completedKeys = [];
  response.results.forEach(function(wrapper) {
    const item = itemByEvent[wrapper.event_id];
    if (!item) return;
    const sheet = SpreadsheetApp.getActive().getSheetByName(item.sheetName);
    const layout = sheetLayout_(item.sheetName);
    const result = wrapper.result || {};
    if (wrapper.status === 'applied' || wrapper.status === 'duplicate') {
      applyNormalizedRow_(sheet, item.rowNumber, result.normalized_row || null);
      sheet.getRange(item.rowNumber, layout.statusColumn).setValue('SYNCED');
      sheet.getRange(item.rowNumber, layout.errorColumn).clearContent();
      completedKeys.push(item.key);
    } else if (wrapper.status === 'conflict') {
      sheet.getRange(item.rowNumber, layout.statusColumn).setValue('CONFLICT');
      sheet.getRange(item.rowNumber, layout.errorColumn).setValue('Конфликт версий');
      completedKeys.push(item.key);
    } else {
      const code = wrapper.error_code || 'BRIDGE_PUSH_ERROR';
      const message = wrapper.error_message || 'Изменение не принято backend.';
      sheet.getRange(item.rowNumber, layout.statusColumn).setValue('ERROR');
      sheet.getRange(item.rowNumber, layout.errorColumn).setValue((code + ': ' + message).slice(0, 500));
      recordSheetError_(item, code, message);
    }
  });
  removeQueuedFinspaceEdits_(completedKeys);
  refreshReferenceLists_();
  const summary = { sent: completedKeys.length, remaining: queuedFinspaceEdits_().length };
  if (notify) SpreadsheetApp.getActive().toast(
    'Обработано: ' + summary.sent + ', осталось: ' + summary.remaining,
    'Финпространство'
  );
  return summary;
}

function pullChanges() {
  return pullChanges_(true);
}

function pullChanges_(notify) {
  const spreadsheet = SpreadsheetApp.getActive();
  const response = signedRequest_('/pull', {
    spreadsheet_id: spreadsheet.getId(),
    limit: FINSPACE.PULL_BATCH_SIZE
  });
  if (response.status === 'paused') {
    if (notify) spreadsheet.toast('Синхронизация приостановлена.', 'Финпространство');
    return { applied: 0, failed: 0 };
  }
  const acknowledgements = [];
  let applied = 0;
  let failed = 0;
  let referencesRefreshed = false;
  orderPulledEvents_(response.events || []).forEach(function(event) {
    let targetRow = 2;
    try {
      if (!referencesRefreshed && event.entity_type === 'transaction') {
        refreshReferenceLists_();
        SpreadsheetApp.flush();
        referencesRefreshed = true;
      }
      const sheet = spreadsheet.getSheetByName(event.sheet_name);
      const layout = sheetLayout_(event.sheet_name);
      if (!sheet || !layout) throw new Error('Неизвестный лист: ' + event.sheet_name);
      const rowNumber = findEntityRow_(sheet, layout.idColumn, String(event.entity_id));
      targetRow = rowNumber || Math.max(sheet.getLastRow() + 1, 2);
      const row = normalizePulledRow_(event.row, layout.width, layout.entityType);
      writePulledRow_(sheet, targetRow, layout.width, row, Boolean(rowNumber));
      acknowledgements.push({
        event_id: event.event_id,
        status: 'applied',
        row_number: targetRow,
        row_hash: event.row_hash
      });
      applied += 1;
    } catch (error) {
      acknowledgements.push({
        event_id: event.event_id,
        status: 'failed',
        error_code: 'APPS_SCRIPT_APPLY_FAILED'
      });
      recordSheetError_({
        sheetName: event.sheet_name,
        rowNumber: targetRow,
        eventId: event.event_id
      }, 'APPS_SCRIPT_APPLY_FAILED', String(error));
      failed += 1;
    }
  });
  if (acknowledgements.length) signedRequest_('/ack', { events: acknowledgements });
  refreshReferenceLists_();
  const meta = spreadsheet.getSheetByName('_sync_meta');
  if (meta) meta.getRange('B6').setValue(new Date().toISOString());
  if (notify) spreadsheet.toast(
    'Получено: ' + applied + ', ошибок: ' + failed,
    'Финпространство'
  );
  return { applied: applied, failed: failed };
}

function orderPulledEvents_(events) {
  const priority = { account: 0, category: 1, transaction: 2 };
  return events.map(function(event, index) {
    return { event: event, index: index };
  }).sort(function(left, right) {
    const leftPriority = Object.prototype.hasOwnProperty.call(priority, left.event.entity_type)
      ? priority[left.event.entity_type] : 99;
    const rightPriority = Object.prototype.hasOwnProperty.call(priority, right.event.entity_type)
      ? priority[right.event.entity_type] : 99;
    return leftPriority - rightPriority || left.index - right.index;
  }).map(function(item) { return item.event; });
}

function writePulledRow_(sheet, targetRow, width, row, restoreExisting) {
  const range = sheet.getRange(targetRow, 1, 1, width);
  const previousValues = range.getValues();
  try {
    withSyncGuard_(function() {
      range.setValues([row]);
      // Apps Script batches writes. Flush now so a validation error belongs to
      // this event instead of being reported against the next event in the batch.
      SpreadsheetApp.flush();
    });
  } catch (error) {
    try {
      withSyncGuard_(function() {
        range.clearContent();
        if (restoreExisting) range.setValues(previousValues);
        SpreadsheetApp.flush();
      });
    } catch (rollbackError) {
      throw new Error(String(error) + '; rollback failed: ' + String(rollbackError));
    }
    throw error;
  }
}

function findEntityRow_(sheet, idColumn, entityId) {
  if (sheet.getLastRow() < 2) return null;
  const values = sheet.getRange(2, idColumn, sheet.getLastRow() - 1, 1).getDisplayValues();
  for (let index = 0; index < values.length; index += 1) {
    if (values[index][0] === entityId) return index + 2;
  }
  return null;
}

function normalizePulledRow_(row, width, entityType) {
  const result = (row || []).slice(0, width);
  while (result.length < width) result.push('');
  const numericIndexes = {
    transaction: [3],
    account: [4, 6, 8],
    category: [5]
  }[entityType] || [];
  numericIndexes.forEach(function(index) {
    if (index < result.length && result[index] !== '' && !isNaN(Number(result[index]))) {
      result[index] = Number(result[index]);
    }
  });
  return result;
}

function heartbeat_() {
  return signedRequest_('/heartbeat', {
    spreadsheet_id: SpreadsheetApp.getActive().getId(),
    apps_script_version: FINSPACE.VERSION
  });
}

function runReconciliation() {
  const spreadsheet = SpreadsheetApp.getActive();
  const snapshotId = Utilities.getUuid();
  const items = collectReconciliationItems_();
  let finalResponse = null;
  if (!items.length) {
    finalResponse = signedRequest_('/reconcile', {
      spreadsheet_id: spreadsheet.getId(),
      snapshot_id: snapshotId,
      items: [],
      final: true
    });
  } else {
    for (let offset = 0; offset < items.length; offset += FINSPACE.RECONCILE_BATCH_SIZE) {
      const batch = items.slice(offset, offset + FINSPACE.RECONCILE_BATCH_SIZE);
      const final = offset + batch.length >= items.length;
      const response = signedRequest_('/reconcile', {
        spreadsheet_id: spreadsheet.getId(),
        snapshot_id: snapshotId,
        items: batch,
        final: final
      });
      if (final) finalResponse = response;
    }
  }
  (finalResponse.actions || []).forEach(function(action) {
    if (action.action !== 'conflict' || !action.row_number) return;
    const sheetName = {
      transaction: 'Операции', account: 'Счета', category: 'Категории'
    }[action.entity_type];
    const sheet = spreadsheet.getSheetByName(sheetName);
    const layout = sheetLayout_(sheetName);
    if (sheet && layout) {
      sheet.getRange(action.row_number, layout.statusColumn).setValue('CONFLICT');
      sheet.getRange(action.row_number, layout.errorColumn).setValue(action.reason);
    }
  });
  const meta = spreadsheet.getSheetByName('_sync_meta');
  if (meta) meta.getRange('B7').setValue(new Date().toISOString());
  pullChanges_(false);
  SpreadsheetApp.getUi().alert(
    'Сверка завершена: ' + JSON.stringify(finalResponse.results || {})
  );
  return finalResponse;
}

function collectReconciliationItems_() {
  const result = [];
  ['Операции', 'Счета', 'Категории'].forEach(function(sheetName) {
    const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
    const layout = sheetLayout_(sheetName);
    if (!sheet || sheet.getLastRow() < 2) return;
    const rows = sheet.getRange(2, 1, sheet.getLastRow() - 1, layout.width).getDisplayValues();
    rows.forEach(function(row, index) {
      const id = row[layout.idColumn - 1];
      const version = Number(row[layout.versionColumn - 1]);
      const hash = row[layout.hashColumn - 1];
      if (!id || !version || !hash) return;
      result.push({
        entity_type: layout.entityType,
        entity_id: id,
        version: version,
        row_hash: hash,
        row_number: index + 2,
        sync_status: row[layout.statusColumn - 1] || 'UNKNOWN'
      });
    });
  });
  return result;
}

function recordSheetError_(item, code, message) {
  const sheet = SpreadsheetApp.getActive().getSheetByName('Ошибки');
  if (!sheet) return;
  sheet.appendRow([
    new Date(), item.sheetName || '', item.rowNumber || '',
    (sheetLayout_(item.sheetName || '') || {}).entityType || '',
    String(message).slice(0, 500), code, 'Исправьте строку и повторите отправку',
    'Да', item.eventId || ''
  ]);
}
