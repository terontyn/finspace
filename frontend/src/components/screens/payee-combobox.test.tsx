import assert from "node:assert/strict";
import test from "node:test";

import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { apiClient } from "@/lib/api-client";
import type { Payee } from "@/types/finance";

import { PayeeCombobox } from "./payee-combobox";

Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", { configurable: true, value: true });

const payee: Payee = {
  aliases: [
    { id: "alias-1", alias: "Кофейня", is_primary: true, created_at: "2026-08-30T00:00:00Z", updated_at: "2026-08-30T00:00:00Z", deleted_at: null },
    { id: "alias-2", alias: "COFFEE", is_primary: false, created_at: "2026-08-30T00:00:00Z", updated_at: "2026-08-30T00:00:00Z", deleted_at: null },
  ],
  created_at: "2026-08-30T00:00:00Z",
  deleted_at: null,
  id: "payee-1",
  name: "Кофейня",
  notes: null,
  updated_at: "2026-08-30T00:00:00Z",
  version: 1,
};

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

test("payee combobox searches on the server and assigns only after explicit selection", async () => {
  const originalGet = apiClient.get;
  const paths: string[] = [];
  const changes: string[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>(path: string) => {
    paths.push(path);
    return Promise.resolve({ items: [payee], page: { limit: 20, offset: 0, total: 1 } } as T);
  }) as typeof apiClient.get;

  try {
    await act(async () => {
      renderer = create(<PayeeCombobox initialSelection={null} onChange={(value) => changes.push(value)} onError={(error) => { throw error; }} value=""/>);
    });
    assert.ok(renderer);
    const input = renderer.root.findByProps({ role: "combobox" });
    await act(async () => { input.props.onFocus(); await settle(); });
    await act(async () => { input.props.onChange({ target: { value: "COFFEE" } }); await settle(); });
    assert.deepEqual(changes, [], "typed search text must not assign a Payee");
    assert.equal(paths.some((path) => path.includes("search=COFFEE")), true);

    await act(async () => { input.props.onKeyDown({ key: "Enter", preventDefault: () => undefined, stopPropagation: () => undefined }); });
    assert.deepEqual(changes, ["payee-1"]);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
  }
});

test("payee combobox preselects an assigned Payee and clears it explicitly", async () => {
  const originalGet = apiClient.get;
  const changes: string[] = [];
  let renderer: ReactTestRenderer | undefined;
  apiClient.get = (<T,>() => Promise.resolve({ items: [payee], page: { limit: 20, offset: 0, total: 1 } } as T)) as typeof apiClient.get;
  try {
    await act(async () => {
      renderer = create(<PayeeCombobox initialSelection={{ id: payee.id, name: payee.name }} onChange={(value) => changes.push(value)} onError={(error) => { throw error; }} value={payee.id}/>);
    });
    assert.ok(renderer);
    const view = renderer;
    assert.equal(view.root.findByProps({ role: "combobox" }).props.value, "Кофейня");
    await act(async () => { view.root.findByProps({ "aria-label": "Очистить получателя" }).props.onClick(); });
    assert.deepEqual(changes, [""]);
  } finally {
    if (renderer) await act(async () => renderer?.unmount());
    apiClient.get = originalGet;
  }
});
