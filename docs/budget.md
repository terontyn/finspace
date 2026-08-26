# Budget backend core

Finspace v0.10 Stage A stores monthly planning data in the backend. The backend is the
source of truth for plans, actual projections, rollover, command history, and Month Close
snapshots. A budget period is identified by workspace, calendar month, and currency.

## Allocation and currency semantics

Each allocation owns exactly one category. A parent allocation does not include child
categories, and a split contributes only to the exact category on that split. New allocations
accept active `expense` and `both` categories. Archived or deleted categories remain visible in
existing history but cannot be added to a plan.

Every currency is projected independently. Stage A does not convert or sum money across
currencies, and workspace `base_currency` is preference metadata rather than a conversion
currency.

## Actuals and formulas

Actuals use the ledger's `confirmed` and `reconciled` transactions in the workspace timezone
with half-open month bounds. Draft, cancelled, and soft-deleted transactions are excluded.
Income and expense refunds reverse the semantic original transaction. Split expense refunds
use the same proportional allocation and deterministic last-split rounding as financial
reports. Transfers are excluded; adjustments are returned separately and participate in net
cash flow.

For each currency group:

```text
allocated = sum(allocation planned amounts)
remaining = allocated - actual_expense
previous_remaining = previous_allocated - previous_actual_expense
planning_capacity = planned_income + rollover_in
unallocated = planning_capacity - allocated
actual_net_cashflow = actual_income - actual_expense + adjustment
```

`rollover_in` is a planning allowance, not account cash. `rollover_policy` is the outgoing
policy owned by this BudgetPeriod: it determines how this period's remaining amount enters the
next month. `rollover.source_policy` is the predecessor policy actually used to calculate the
current incoming rollover. `none` carries zero, `positive_only` carries only the positive part,
and `full` carries the signed amount. Live rollover is marked provisional while its predecessor
can still be created, restored, or changed. A frozen predecessor is read only from its immutable
Month Close revision. Negative `remaining` and `unallocated` values are valid.

## Commands, history, and permissions

`PUT` replaces the complete allocation collection atomically. Update, delete, restore, and copy
use optimistic versions. Every successful mutation requires `X-Idempotency-Key`; retrying the
same semantic request returns its original stored response even after later mutations. Reusing a
key for another request returns `BUDGET_IDEMPOTENCY_CONFLICT`.

Delete is a soft delete of the period. Allocations remain as restore and audit evidence. Restore
keeps the same period ID and rejects plans whose categories are no longer valid. Immutable plan
revisions and aggregate audit snapshots include all allocations. Viewers can read budgets and
history; editors and owners can mutate them. Service accounts have no Stage A Budget endpoint.
Copy revisions retain the domain action `copy`; aggregate audit uses the existing `create` or
`update` action and records `budget_operation=copy` plus source-period provenance.

## Month Close

Month Close may proceed when no budget exists. Prepare records a canonical
`planning_budget` snapshot and a separate `budget_plan_fingerprint`; the existing financial
fingerprint keeps its original meaning. A plan change after prepare makes confirmation stale.
Confirmed months read their historical projection from the current immutable Month Close
revision and reject mutations with `BUDGET_PERIOD_FROZEN`. Owner reopen makes the live plan
editable again; a later confirm creates a new immutable close snapshot without changing older
revisions.

The canonical lock order is Month Close control, Month Closure when used, then Budget periods in
deterministic order.

Budget frontend, Goals, forecasts, Google Sheets/Apps Script synchronization, n8n workflows, and
notifications are outside Stage A.
