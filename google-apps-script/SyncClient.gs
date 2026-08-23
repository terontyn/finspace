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
  let response;
  try {
    response = UrlFetchApp.fetch(
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
  } catch (error) {
    throw finspaceRequestError_(
      'BRIDGE_NETWORK_ERROR',
      'Сетевой запрос к backend не выполнен: ' + safeFinspaceErrorMessage_(error),
      null,
      true
    );
  }
  const responseCode = response.getResponseCode();
  let parsed = {};
  try {
    parsed = JSON.parse(response.getContentText() || '{}');
  } catch (error) {
    throw finspaceRequestError_(
      'BRIDGE_RESPONSE_INVALID',
      'Backend вернул ответ не в JSON. HTTP ' + responseCode,
      responseCode,
      isRetryableBridgeHttpStatus_(responseCode)
    );
  }
  if (responseCode < 200 || responseCode >= 300) {
    const apiError = parsed.error || {};
    throw finspaceRequestError_(
      apiError.code || 'BRIDGE_HTTP_ERROR',
      apiError.message || ('HTTP ' + responseCode),
      responseCode,
      isRetryableBridgeHttpStatus_(responseCode) ||
        (apiError.code === 'APPS_SCRIPT_REPLAY_DETECTED')
    );
  }
  return parsed;
}

function pushPendingChanges() {
  return pushPendingChanges_(true);
}

function pushPendingChanges_(notify) {
  const syncLock = LockService.getScriptLock();
  if (notify) {
    syncLock.waitLock(FINSPACE.QUEUE_LOCK_TIMEOUT_MS);
  } else if (!syncLock.tryLock(1000)) {
    return { sent: 0, remaining: queuedFinspaceEdits_().length, skipped: true };
  }
  try {
    return pushPendingChangesUnlocked_(notify);
  } finally {
    syncLock.releaseLock();
  }
}

function pushPendingChangesUnlocked_(notify) {
  recoverPendingFinspaceEdits_();
  const queue = queuedFinspaceEdits_().slice(0, FINSPACE.PUSH_BATCH_SIZE);
  if (!queue.length) {
    if (notify) SpreadsheetApp.getActive().toast('Локальная очередь пуста.', 'Финпространство');
    return { sent: 0, remaining: 0 };
  }
  const prepared = [];
  queue.forEach(function(item) {
    try {
      const payload = payloadForEdit_(item);
      prepared.push({
        item: item,
        payload: payload,
        identity: finspacePayloadIdentity_(payload)
      });
    } catch (error) {
      setQueuedRowState_(item, 'ERROR', 'LOCAL_VALIDATION_ERROR: ' + safeFinspaceErrorMessage_(error));
      recordSheetError_(item, 'LOCAL_VALIDATION_ERROR', String(error));
    }
  });
  const reserved = reserveFinspacePushItems_(prepared);
  if (!reserved.length) return { sent: 0, remaining: queuedFinspaceEdits_().length };

  let response;
  try {
    response = signedRequest_('/push', {
      events: reserved.map(function(item) { return item.payload; })
    });
  } catch (error) {
    markFinspacePushFailure_(reserved, error);
    throw error;
  }
  if (!response || !Array.isArray(response.results)) {
    const invalid = finspaceRequestError_(
      'BRIDGE_RESPONSE_INVALID',
      'Backend не подтвердил результаты push.',
      200,
      true
    );
    markFinspacePushFailure_(reserved, invalid);
    throw invalid;
  }
  const completed = commitFinspacePushResults_(reserved, response.results);
  refreshReferenceLists_();
  const summary = { sent: completed, remaining: queuedFinspaceEdits_().length };
  if (notify) SpreadsheetApp.getActive().toast(
    'Обработано: ' + summary.sent + ', осталось: ' + summary.remaining,
    'Финпространство'
  );
  return summary;
}

function reserveFinspacePushItems_(prepared) {
  return withFinspaceQueueLock_(function() {
    const queue = readFinspaceQueueOrRecoverUnsafe_();
    const currentByKey = {};
    queue.forEach(function(item) { currentByKey[item.key] = item; });
    const now = new Date().toISOString();
    const reserved = [];
    prepared.forEach(function(candidate) {
      const current = currentByKey[candidate.item.key];
      if (!current || current.eventId !== candidate.item.eventId) return;
      current.attemptCount = Number(current.attemptCount || 0) + 1;
      current.lastAttemptAt = now;
      writeQueueEventNote_(current.sheetName, current.rowNumber, current.eventId);
      setQueuedRowState_(current, 'PENDING', '');
      reserved.push({
        item: Object.assign({}, current),
        payload: candidate.payload,
        identity: candidate.identity
      });
    });
    writeFinspaceQueueUnsafe_(queue);
    return reserved;
  });
}

function commitFinspacePushResults_(reserved, results) {
  return withFinspaceQueueLock_(function() {
    const queue = readFinspaceQueueOrRecoverUnsafe_();
    const resultByEvent = {};
    results.forEach(function(wrapper) {
      if (wrapper && typeof wrapper.event_id === 'string' && !resultByEvent[wrapper.event_id]) {
        resultByEvent[wrapper.event_id] = wrapper;
      }
    });
    let completed = 0;
    reserved.forEach(function(candidate) {
      const index = queue.findIndex(function(item) {
        return item.key === candidate.item.key && item.eventId === candidate.item.eventId;
      });
      if (index < 0) return;
      const item = queue[index];
      const wrapper = resultByEvent[item.eventId];
      if (!wrapper) {
        setQueuedRowState_(item, 'PENDING', 'Backend не подтвердил это событие; будет повторено.');
        return;
      }
      const result = wrapper.result || null;
      if (wrapper.status === 'applied' || wrapper.status === 'duplicate') {
        if (!isConfirmedFinspaceSuccess_(item, wrapper)) {
          setQueuedRowState_(item, 'PENDING', 'Некорректное подтверждение backend; будет повторено.');
          return;
        }
        const sheet = SpreadsheetApp.getActive().getSheetByName(item.sheetName);
        let currentIdentity = '';
        try {
          currentIdentity = finspacePayloadIdentity_(payloadForEdit_(item));
        } catch (error) {
          currentIdentity = '__changed__';
        }
        if (currentIdentity !== candidate.identity) {
          applyNormalizedTechnicalRow_(sheet, item.rowNumber, result.normalized_row);
          const replacement = Object.assign({}, item, {
            eventId: Utilities.getUuid(),
            queuedAt: new Date().toISOString(),
            attemptCount: 0,
            lastAttemptAt: null
          });
          queue[index] = replacement;
          writeQueueEventNote_(replacement.sheetName, replacement.rowNumber, replacement.eventId);
          setQueuedRowState_(replacement, 'DIRTY', 'Изменение во время отправки поставлено повторно.');
          return;
        }
        applyNormalizedRow_(sheet, item.rowNumber, result.normalized_row);
        setQueuedRowState_(item, 'SYNCED', '');
        clearQueueEventNote_(item.sheetName, item.rowNumber, item.eventId);
        queue.splice(index, 1);
        completed += 1;
        return;
      }
      if (wrapper.status === 'conflict') {
        setQueuedRowState_(item, 'CONFLICT', 'Конфликт версий');
        clearQueueEventNote_(item.sheetName, item.rowNumber, item.eventId);
        queue.splice(index, 1);
        completed += 1;
        return;
      }
      if (wrapper.status === 'rejected') {
        const code = wrapper.error_code || 'BRIDGE_PUSH_REJECTED';
        const message = wrapper.error_message || 'Изменение окончательно отклонено backend.';
        setQueuedRowState_(item, 'ERROR', code + ': ' + message);
        recordSheetError_(item, code, message);
        clearQueueEventNote_(item.sheetName, item.rowNumber, item.eventId);
        queue.splice(index, 1);
        completed += 1;
        return;
      }
      setQueuedRowState_(item, 'PENDING', 'Неизвестный результат backend; будет повторено.');
    });
    writeFinspaceQueueUnsafe_(queue);
    return completed;
  });
}

function markFinspacePushFailure_(reserved, error) {
  const retryable = !error || error.retryable !== false;
  const status = retryable ? 'PENDING' : 'ERROR';
  const message = (error && error.code ? error.code + ': ' : '') + safeFinspaceErrorMessage_(error);
  withFinspaceQueueLock_(function() {
    const queue = readFinspaceQueueOrRecoverUnsafe_();
    reserved.forEach(function(candidate) {
      const current = queue.filter(function(item) {
        return item.key === candidate.item.key && item.eventId === candidate.item.eventId;
      })[0];
      if (current) setQueuedRowState_(current, status, message);
    });
  });
}

function setQueuedRowState_(item, status, message) {
  const sheet = SpreadsheetApp.getActive().getSheetByName(item.sheetName);
  const layout = sheetLayout_(item.sheetName);
  if (!sheet || !layout) return;
  sheet.getRange(item.rowNumber, layout.statusColumn).setValue(status);
  const errorRange = sheet.getRange(item.rowNumber, layout.errorColumn);
  if (message) {
    errorRange.setValue(String(message).slice(0, 500));
  } else {
    errorRange.clearContent();
  }
}

function finspacePayloadIdentity_(payload) {
  return JSON.stringify({
    sheet_name: payload.sheet_name,
    row_number: payload.row_number,
    entity_type: payload.entity_type,
    entity_id: payload.entity_id,
    expected_version: payload.expected_version,
    changed_fields: payload.changed_fields,
    visible_row: payload.visible_row
  });
}

function isConfirmedFinspaceSuccess_(item, wrapper) {
  const result = wrapper.result;
  return Boolean(
    result &&
    result.event_id === item.eventId &&
    result.normalized_row &&
    typeof result.normalized_row === 'object' &&
    !Array.isArray(result.normalized_row)
  );
}

function isRetryableBridgeHttpStatus_(status) {
  return status === 408 || status === 425 || status === 429 ||
    status === 500 || status === 502 || status === 503 || status === 504;
}

function finspaceRequestError_(code, message, httpStatus, retryable) {
  const error = new Error(code + ': ' + message);
  error.code = code;
  error.httpStatus = httpStatus;
  error.retryable = retryable;
  return error;
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
