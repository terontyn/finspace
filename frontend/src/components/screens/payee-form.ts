import type { Payee, PayeeAlias } from "@/types/finance";

export interface PayeeForm {
  name: string;
  notes: string;
}

export function initialPayeeForm(): PayeeForm {
  return { name: "", notes: "" };
}

export function payeeFormFromRecord(payee: Payee): PayeeForm {
  return { name: payee.name, notes: payee.notes ?? "" };
}

export function payeeMutation(form: PayeeForm, editing: Payee | null) {
  const body = { name: form.name.trim(), notes: form.notes.trim() || null };
  return editing
    ? {
        method: "PATCH" as const,
        path: `/api/v1/payees/${editing.id}`,
        body: { ...body, version: editing.version },
      }
    : { method: "POST" as const, path: "/api/v1/payees", body };
}

export function payeeArchiveMutation(payee: Payee) {
  return { path: `/api/v1/payees/${payee.id}?version=${payee.version}` };
}

export function payeeRestoreMutation(payee: Payee) {
  return { path: `/api/v1/payees/${payee.id}/restore`, body: { version: payee.version } };
}

export function payeeAliasCreateMutation(payee: Payee, alias: string) {
  return {
    path: `/api/v1/payees/${payee.id}/aliases`,
    body: { alias: alias.trim(), version: payee.version },
  };
}

export function payeeAliasArchiveMutation(payee: Payee, alias: PayeeAlias) {
  return {
    path: `/api/v1/payees/${payee.id}/aliases/${alias.id}?version=${payee.version}`,
  };
}

export function payeeAliasRestoreMutation(payee: Payee, alias: PayeeAlias) {
  return {
    path: `/api/v1/payees/${payee.id}/aliases/${alias.id}/restore`,
    body: { version: payee.version },
  };
}
