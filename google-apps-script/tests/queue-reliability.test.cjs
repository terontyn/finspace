const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');

class MemoryProperties {
  constructor() {
    this.values = new Map();
  }

  getProperty(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }

  setProperty(key, value) {
    this.values.set(key, String(value));
    return this;
  }

  setProperties(values) {
    Object.entries(values).forEach(([key, value]) => this.setProperty(key, value));
    return this;
  }

  deleteProperty(key) {
    this.values.delete(key);
    return this;
  }
}

class MemoryLock {
  constructor(name) {
    this.name = name;
    this.locked = false;
  }

  waitLock() {
    if (this.locked) throw new Error(`re-entrant ${this.name} lock`);
    this.locked = true;
  }

  tryLock() {
    if (this.locked) return false;
    this.locked = true;
    return true;
  }

  releaseLock() {
    if (!this.locked) throw new Error(`unlocked ${this.name} lock`);
    this.locked = false;
  }
}

class MemoryRange {
  constructor(sheet, row, column, rowCount = 1, columnCount = 1) {
    this.sheet = sheet;
    this.row = row;
    this.column = column;
    this.rowCount = rowCount;
    this.columnCount = columnCount;
  }

  getValues() {
    return this.sheet.read(this.row, this.column, this.rowCount, this.columnCount);
  }

  getDisplayValues() {
    return this.getValues().map((row) => row.map((value) => value == null ? '' : String(value)));
  }

  getDisplayValue() {
    return this.getDisplayValues()[0][0];
  }

  setValue(value) {
    this.sheet.write(this.row, this.column, [[value]]);
    return this;
  }

  setValues(values) {
    this.sheet.write(this.row, this.column, values);
    return this;
  }

  clearContent() {
    const values = Array.from({ length: this.rowCount }, () =>
      Array.from({ length: this.columnCount }, () => '')
    );
    this.sheet.write(this.row, this.column, values);
    return this;
  }

  getNote() {
    return this.sheet.notes.get(`${this.row}:${this.column}`) || '';
  }

  setNote(value) {
    this.sheet.notes.set(`${this.row}:${this.column}`, String(value));
    return this;
  }

  getSheet() { return this.sheet; }
  getColumn() { return this.column; }
  getLastColumn() { return this.column + this.columnCount - 1; }
  getRow() { return this.row; }
  getLastRow() { return this.row + this.rowCount - 1; }
}

class MemorySheet {
  constructor(name, width) {
    this.name = name;
    this.width = width;
    this.rows = [Array.from({ length: width }, (_, index) => `header-${index + 1}`)];
    this.notes = new Map();
  }

  ensure(row, column) {
    while (this.rows.length < row) this.rows.push(Array(this.width).fill(''));
    while (this.rows[row - 1].length < column) this.rows[row - 1].push('');
  }

  read(row, column, rowCount, columnCount) {
    const values = [];
    for (let rowOffset = 0; rowOffset < rowCount; rowOffset += 1) {
      const current = [];
      for (let columnOffset = 0; columnOffset < columnCount; columnOffset += 1) {
        this.ensure(row + rowOffset, column + columnOffset);
        current.push(this.rows[row + rowOffset - 1][column + columnOffset - 1]);
      }
      values.push(current);
    }
    return values;
  }

  write(row, column, values) {
    values.forEach((current, rowOffset) => {
      current.forEach((value, columnOffset) => {
        this.ensure(row + rowOffset, column + columnOffset);
        this.rows[row + rowOffset - 1][column + columnOffset - 1] = value;
      });
    });
  }

  getRange(row, column, rowCount = 1, columnCount = 1) {
    return new MemoryRange(this, row, column, rowCount, columnCount);
  }

  getLastRow() { return this.rows.length; }
  getMaxRows() { return Math.max(this.rows.length, 100); }
  getLastColumn() { return this.width; }
  getName() { return this.name; }
  appendRow(values) { this.rows.push([...values]); }
}

function response(status, payload) {
  return {
    getResponseCode: () => status,
    getContentText: () => typeof payload === 'string' ? payload : JSON.stringify(payload),
  };
}

function createHarness(fetchImpl) {
  const properties = new MemoryProperties();
  properties.setProperties({
    FINSPACE_BACKEND_URL: 'https://backend.example.test',
    FINSPACE_BINDING_ID: 'binding-id',
    FINSPACE_BINDING_SECRET: 'secret',
  });
  const documentLock = new MemoryLock('document');
  const scriptLock = new MemoryLock('script');
  const sheets = new Map([
    ['Операции', new MemorySheet('Операции', 27)],
    ['Счета', new MemorySheet('Счета', 17)],
    ['Категории', new MemorySheet('Категории', 16)],
    ['Ошибки', new MemorySheet('Ошибки', 9)],
  ]);
  const spreadsheet = {
    getSheetByName: (name) => sheets.get(name) || null,
    getId: () => 'spreadsheet-id',
    getUrl: () => 'https://docs.google.test/spreadsheet-id',
    getSpreadsheetTimeZone: () => 'UTC',
    toast: () => {},
  };
  let uuid = 0;
  const context = vm.createContext({
    console,
    Date,
    Error,
    JSON,
    Math,
    Number,
    Object,
    String,
    Array,
    RegExp,
    PropertiesService: { getDocumentProperties: () => properties },
    LockService: {
      getDocumentLock: () => documentLock,
      getScriptLock: () => scriptLock,
    },
    SpreadsheetApp: {
      getActive: () => spreadsheet,
      flush: () => {},
    },
    Utilities: {
      DigestAlgorithm: { SHA_256: 'SHA_256' },
      Charset: { UTF_8: 'UTF_8' },
      getUuid: () => `event-${++uuid}`,
      computeDigest: () => [1, 2, 3],
      computeHmacSha256Signature: () => [4, 5, 6],
      newBlob: (value) => ({ getBytes: () => [...Buffer.from(String(value))] }),
    },
    UrlFetchApp: { fetch: (...args) => fetchImpl(...args) },
  });
  for (const file of ['Config.gs', 'EditQueue.gs', 'SyncClient.gs']) {
    vm.runInContext(
      fs.readFileSync(path.join(ROOT, 'google-apps-script', file), 'utf8'),
      context,
      { filename: file }
    );
  }
  context.refreshReferenceLists_ = () => {};
  context.payloadForEdit_ = (item) => {
    const sheet = spreadsheet.getSheetByName(item.sheetName);
    const row = sheet.read(item.rowNumber, 1, 1, 27)[0];
    return {
      event_id: item.eventId,
      spreadsheet_id: 'spreadsheet-id',
      sheet_name: item.sheetName,
      row_number: item.rowNumber,
      entity_type: 'transaction',
      entity_id: row[18] || null,
      expected_version: row[23] ? Number(row[23]) : null,
      row_hash: row[24] || null,
      changed_fields: { description: row[10] },
      visible_row: { description: row[10] },
    };
  };
  context.applyNormalizedRow_ = (sheet, rowNumber, normalized) => {
    Object.entries(normalized || {}).forEach(([index, value]) => {
      sheet.write(rowNumber, Number(index) + 1, [[value]]);
    });
  };
  context.applyNormalizedTechnicalRow_ = (sheet, rowNumber, normalized) => {
    Object.entries(normalized || {}).forEach(([index, value]) => {
      if (Number(index) + 1 >= 19) sheet.write(rowNumber, Number(index) + 1, [[value]]);
    });
  };
  return { context, properties, sheets, spreadsheet };
}

function seedTransaction(harness, rowNumber = 2, description = 'changed') {
  const sheet = harness.sheets.get('Операции');
  sheet.write(rowNumber, 11, [[description]]);
  sheet.write(rowNumber, 17, [['DIRTY']]);
  sheet.write(rowNumber, 19, [[`transaction-${rowNumber}`]]);
  sheet.write(rowNumber, 24, [[1]]);
  sheet.write(rowNumber, 25, [['old-hash']]);
  return harness.context.enqueueFinspaceEdit_('Операции', rowNumber);
}

function successfulPush(body, status = 'applied') {
  const event = JSON.parse(body).events[0];
  return response(200, {
    results: [{
      event_id: event.event_id,
      status,
      result: {
        status,
        event_id: event.event_id,
        entity_id: event.entity_id || 'created-id',
        version: 2,
        row_hash: 'new-hash',
        normalized_row: {
          10: event.changed_fields.description,
          18: event.entity_id || 'created-id',
          23: 2,
          24: 'new-hash',
        },
      },
    }],
  });
}

test('DNS failure keeps the exact event queued and PENDING', () => {
  const harness = createHarness(() => { throw new Error('DNS lookup failed'); });
  const item = seedTransaction(harness);
  assert.throws(() => harness.context.pushPendingChanges_(false), /BRIDGE_NETWORK_ERROR/);
  assert.equal(harness.context.queuedFinspaceEdits_()[0].eventId, item.eventId);
  assert.equal(harness.sheets.get('Операции').read(2, 17, 1, 1)[0][0], 'PENDING');
});

test('HTTP 503 keeps the event queued for retry', () => {
  const harness = createHarness(() => response(503, { error: { code: 'TEMPORARY', message: 'retry' } }));
  seedTransaction(harness);
  assert.throws(() => harness.context.pushPendingChanges_(false), /TEMPORARY/);
  assert.equal(harness.context.queuedFinspaceEdits_().length, 1);
  assert.equal(harness.sheets.get('Операции').read(2, 17, 1, 1)[0][0], 'PENDING');
});

test('timeout keeps the event queued for retry', () => {
  const harness = createHarness(() => { throw new Error('execution timed out'); });
  seedTransaction(harness);
  assert.throws(() => harness.context.pushPendingChanges_(false), /BRIDGE_NETWORK_ERROR/);
  assert.equal(harness.context.queuedFinspaceEdits_().length, 1);
});

test('successful 2xx dequeues only after a valid per-event confirmation', () => {
  let valid = false;
  const harness = createHarness((_url, options) => valid
    ? successfulPush(options.payload)
    : response(200, { status: 'ok' }));
  seedTransaction(harness);
  assert.throws(() => harness.context.pushPendingChanges_(false), /BRIDGE_RESPONSE_INVALID/);
  assert.equal(harness.context.queuedFinspaceEdits_().length, 1);
  valid = true;
  assert.deepEqual(
    JSON.parse(JSON.stringify(harness.context.pushPendingChanges_(false))),
    { sent: 1, remaining: 0 }
  );
  assert.equal(harness.sheets.get('Операции').read(2, 17, 1, 1)[0][0], 'SYNCED');
});

test('invalid signature is visible but keeps the business event', () => {
  const harness = createHarness(() => response(401, {
    error: { code: 'APPS_SCRIPT_SIGNATURE_INVALID', message: 'invalid signature' },
  }));
  seedTransaction(harness);
  assert.throws(() => harness.context.pushPendingChanges_(false), /APPS_SCRIPT_SIGNATURE_INVALID/);
  assert.equal(harness.context.queuedFinspaceEdits_().length, 1);
  assert.equal(harness.sheets.get('Операции').read(2, 17, 1, 1)[0][0], 'ERROR');
});

test('replay response retries with the same event identity and a new HTTP nonce', () => {
  const nonces = [];
  let attempt = 0;
  const harness = createHarness((_url, options) => {
    nonces.push(options.headers['X-Finspace-Nonce']);
    attempt += 1;
    if (attempt === 1) {
      return response(409, {
        error: { code: 'APPS_SCRIPT_REPLAY_DETECTED', message: 'replay' },
      });
    }
    return successfulPush(options.payload);
  });
  const item = seedTransaction(harness);
  assert.throws(() => harness.context.pushPendingChanges_(false), /APPS_SCRIPT_REPLAY_DETECTED/);
  assert.equal(harness.context.queuedFinspaceEdits_()[0].eventId, item.eventId);
  harness.context.pushPendingChanges_(false);
  assert.equal(harness.context.queuedFinspaceEdits_().length, 0);
  assert.notEqual(nonces[0], nonces[1]);
});

test('a terminal failure for the first item does not remove an unconfirmed second item', () => {
  const harness = createHarness((_url, options) => {
    const events = JSON.parse(options.payload).events;
    return response(200, {
      results: [{
        event_id: events[0].event_id,
        status: 'rejected',
        error_code: 'VALIDATION_ERROR',
        error_message: 'invalid row',
      }],
    });
  });
  seedTransaction(harness, 2, 'first');
  const second = seedTransaction(harness, 3, 'second');
  const result = harness.context.pushPendingChanges_(false);
  assert.equal(result.sent, 1);
  assert.equal(harness.context.queuedFinspaceEdits_().length, 1);
  assert.equal(harness.context.queuedFinspaceEdits_()[0].eventId, second.eventId);
  assert.equal(harness.sheets.get('Операции').read(3, 17, 1, 1)[0][0], 'PENDING');
});

test('an edit enqueued during HTTP cannot be removed by the older response', () => {
  let harness;
  let replacement;
  harness = createHarness((_url, options) => {
    const sheet = harness.sheets.get('Операции');
    sheet.write(2, 11, [['newer edit']]);
    sheet.write(2, 17, [['DIRTY']]);
    replacement = harness.context.enqueueFinspaceEdit_('Операции', 2);
    return successfulPush(options.payload);
  });
  const original = seedTransaction(harness, 2, 'original edit');
  harness.context.pushPendingChanges_(false);
  const queue = harness.context.queuedFinspaceEdits_();
  assert.equal(queue.length, 1);
  assert.equal(queue[0].eventId, replacement.eventId);
  assert.notEqual(queue[0].eventId, original.eventId);
  assert.equal(harness.sheets.get('Операции').read(2, 11, 1, 1)[0][0], 'newer edit');
});

test('PENDING row with a durable event note rebuilds a missing property queue', () => {
  const harness = createHarness(() => { throw new Error('not called'); });
  const item = seedTransaction(harness);
  harness.properties.setProperty('FINSPACE_EDIT_QUEUE_V1', '[]');
  harness.sheets.get('Операции').write(2, 17, [['PENDING']]);
  assert.equal(harness.context.recoverPendingFinspaceEdits_(), 1);
  const recovered = harness.context.queuedFinspaceEdits_();
  assert.equal(recovered.length, 1);
  assert.equal(recovered[0].eventId, item.eventId);
});

test('ambiguous first response retries idempotently and canonical duplicate confirmation dequeues', () => {
  let backendMutations = 0;
  let first = true;
  const harness = createHarness((_url, options) => {
    if (first) {
      first = false;
      backendMutations += 1;
      throw new Error('connection reset after backend commit');
    }
    return successfulPush(options.payload, 'duplicate');
  });
  seedTransaction(harness);
  assert.throws(() => harness.context.pushPendingChanges_(false), /BRIDGE_NETWORK_ERROR/);
  harness.context.pushPendingChanges_(false);
  assert.equal(backendMutations, 1);
  assert.equal(harness.context.queuedFinspaceEdits_().length, 0);
  assert.equal(harness.sheets.get('Операции').read(2, 24, 1, 1)[0][0], 2);
});
