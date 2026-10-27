import assert from "node:assert/strict";
import test, { after } from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { ApiClientError, apiClient } from "@/lib/api-client";
import type {
  Account,
  AccountReconciliation,
  AccountReconciliationPreview,
} from "@/types/finance";

import { AccountReconciliationDialog, isZeroMoney } from "./account-reconciliation-dialog";

const originalActEnvironment = Object.getOwnPropertyDescriptor(
  globalThis,
  "IS_REACT_ACT_ENVIRONMENT",
);
Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", {
  configurable: true,
  value: true,
});
after(() => {
  if (originalActEnvironment) {
    Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", originalActEnvironment);
  } else {
    Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
  }
});

const account: Account = {
  account_type: "debit_card",
  created_at: "2026-08-01T00:00:00.000Z",
  credit_limit: null,
  currency: "RUB",
  description: null,
  id: "account-1",
  institution: "Банк",
  is_archived: false,
  name: "Основной",
  opening_balance: "100.0000",
  opening_balance_at: "2026-08-01T00:00:00.000Z",
  updated_at: "2026-08-20T00:00:00.000Z",
  version: 2,
};

const preview: AccountReconciliationPreview = {
  account_id: account.id,
  account_version: account.version,
  calculated_balance: "125.0000",
  currency: "RUB",
  cutoff_at: "2026-08-21T00:00:00.000Z",
  difference: "0.0000",
  preview_token: "a".repeat(64),
  statement_balance: "125.0000",
  statement_date: "2026-08-20",
  transactions: [{
    amount: "25.0000",
    counterparty: "Работодатель",
    currency: "RUB",
    description: "Зарплата",
    id: "transaction-1",
    occurred_at: "2026-08-20T10:00:00.000Z",
    signed_amount: "25.0000",
    status: "confirmed",
    transaction_type: "income",
    version: 1,
  }],
};

const confirmed: AccountReconciliation = {
  account_id: account.id,
  account_version: account.version,
  calculated_balance: preview.calculated_balance,
  confirmed_at: "2026-08-21T10:00:00.000Z",
  confirmed_by: "user-1",
  created_at: "2026-08-21T10:00:00.000Z",
  created_by: "user-1",
  currency: "RUB",
  difference: "0.0000",
  id: "reconciliation-1",
  statement_balance: preview.statement_balance,
  statement_date: preview.statement_date,
  status: "confirmed",
  transaction_ids: ["transaction-1"],
  version: 1,
};

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
}

async function fillAndPreview(renderer: ReactTestRenderer) {
  const dateInput = renderer.root.findByProps({ type: "date" });
  const balanceInput = renderer.root.findByProps({ type: "number" });
  await act(async () => {
    dateInput.props.onChange({ target: { value: preview.statement_date } });
    balanceInput.props.onChange({ target: { value: preview.statement_balance } });
  });
  const form = renderer.root.findByProps({ className: "reconciliation-form" });
  await act(async () => {
    form.props.onSubmit({ preventDefault: () => undefined });
    await settle();
  });
}

test("money zero check does not use floating-point calculations", () => {
  assert.equal(isZeroMoney("0.0000"), true);
  assert.equal(isZeroMoney("-0.00"), true);
  assert.equal(isZeroMoney("0.0001"), false);
  assert.equal(isZeroMoney("-1.0000"), false);
});

test("reconciliation dialog previews and confirms through backend exactly once", async () => {
  const originalPost = apiClient.post;
  const requests: Array<{ path: string; body: unknown }> = [];
  const results: AccountReconciliation[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.post = (<T,>(path: string, body?: unknown) => {
    requests.push({ path, body });
    if (path.endsWith("/preview")) return Promise.resolve(preview as T);
    if (path.endsWith("/confirm")) return Promise.resolve(confirmed as T);
    throw new Error(`Unexpected request: ${path}`);
  }) as typeof apiClient.post;

  try {
    await act(async () => {
      renderer = create(<AccountReconciliationDialog account={account} onClose={() => undefined} onConfirmed={(result) => { results.push(result); }} onError={(error) => { throw error; }} timezone="UTC"/>);
    });
    if (!renderer) throw new Error("Renderer was not created");
    const view = renderer;
    await fillAndPreview(view);
    const output = JSON.stringify(view.toJSON());
    assert.match(output, /Баланс совпадает/);
    assert.match(output, /Работодатель/);
    assert.deepEqual(requests[0].body, {
      account_version: 2,
      currency: "RUB",
      statement_balance: "125.0000",
      statement_date: "2026-08-20",
    });

    const confirmButton = view.root.findByProps({ children: "Подтвердить сверку" });
    assert.equal(confirmButton.props.disabled, false);
    await act(async () => {
      confirmButton.props.onClick();
      await settle();
    });
    assert.equal(requests.filter((request) => request.path.endsWith("/confirm")).length, 1);
    assert.equal(results[0]?.id, confirmed.id);
    assert.match(JSON.stringify(view.toJSON()), /Сверка подтверждена/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.post = originalPost;
  }
});

test("non-zero difference blocks confirmation and never creates a hidden adjustment", async () => {
  const originalPost = apiClient.post;
  let renderer: ReactTestRenderer | undefined;
  apiClient.post = (<T,>(path: string) => {
    if (path.endsWith("/preview")) return Promise.resolve({ ...preview, difference: "5.0000", statement_balance: "130.0000" } as T);
    throw new Error("Confirm must not be called for a non-zero difference");
  }) as typeof apiClient.post;

  try {
    await act(async () => {
      renderer = create(<AccountReconciliationDialog account={account} onClose={() => undefined} onConfirmed={() => undefined} onError={(error) => { throw error; }} timezone="UTC"/>);
    });
    if (!renderer) throw new Error("Renderer was not created");
    const view = renderer;
    await fillAndPreview(view);
    const confirmButton = view.root.findByProps({ children: "Подтвердить сверку" });
    assert.equal(confirmButton.props.disabled, true);
    const output = JSON.stringify(view.toJSON());
    assert.match(output, /Есть расхождение/);
    assert.match(output, /Автоматическая корректировка не создаётся/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.post = originalPost;
  }
});

test("409 confirm is shown without an automatic retry", async () => {
  const originalPost = apiClient.post;
  let confirmCalls = 0;
  let renderer: ReactTestRenderer | undefined;
  apiClient.post = (<T,>(path: string) => {
    if (path.endsWith("/preview")) return Promise.resolve(preview as T);
    confirmCalls += 1;
    return Promise.reject(new ApiClientError("Stale preview", "RECONCILIATION_PREVIEW_STALE", 409));
  }) as typeof apiClient.post;

  try {
    await act(async () => {
      renderer = create(<AccountReconciliationDialog account={account} onClose={() => undefined} onConfirmed={() => undefined} onError={(error) => { throw error; }} timezone="UTC"/>);
    });
    if (!renderer) throw new Error("Renderer was not created");
    const view = renderer;
    await fillAndPreview(view);
    await act(async () => {
      view.root.findByProps({ children: "Подтвердить сверку" }).props.onClick();
      await settle();
    });
    assert.equal(confirmCalls, 1);
    assert.match(JSON.stringify(view.toJSON()), /Автоповтор отключён/);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.post = originalPost;
  }
});
