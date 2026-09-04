#!/bin/sh
# Read-only probe of one Finspace database, for the disaster-recovery drill.
#
# It answers exactly one question: did the financial state survive the restore. So it emits row
# counts and one timestamp, and nothing else — no names, descriptions, memos, emails, account
# numbers, tokens or secrets ever leave this script. The output is meant to be committed to an
# acceptance artifact and read by a human, so it must stay safe to look at.
#
# Runs inside the tools container, where psql and the database credentials already live. It opens
# a read-only transaction and writes nothing: a probe that mutates the database it is measuring
# would invalidate the comparison it exists to make.
#
# Two modes:
#   --schema-state   how many public tables and which Alembic revision (works on an empty database)
#   (default)        the full JSON probe (requires the schema to be present)
set -eu

usage() {
  echo "Usage: dr-data-probe.sh [--schema-state] [DATABASE]" >&2
}

mode="probe"
if [ "${1:-}" = "--schema-state" ]; then
  mode="schema"
  shift
fi
[ "$#" -le 1 ] || { usage; exit 2; }

database="${1:-${PGDATABASE:?PGDATABASE is required}}"
case "$database" in
  ''|*[!A-Za-z0-9_]*) echo "Probe refused: unsafe database name." >&2; exit 1 ;;
esac

if [ "$mode" = "schema" ]; then
  tables="$(psql -XAtq -d "$database" -c \
    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
  revision="$(psql -XAtq -d "$database" -c \
    "SELECT coalesce((SELECT version_num FROM alembic_version LIMIT 1), '')" 2>/dev/null || true)"
  printf 'tables=%s\n' "$tables"
  printf 'revision=%s\n' "$revision"
  exit 0
fi

# READ ONLY is declarative, not decorative: it makes an accidental write in this file impossible
# rather than merely unlikely.
psql -XAtq -d "$database" <<'SQL'
BEGIN READ ONLY;
SELECT jsonb_pretty(jsonb_build_object(
  'version', 1,
  'captured_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'alembic_revision', (SELECT version_num FROM alembic_version LIMIT 1),
  -- Compared strictly between source and restored database.
  'compared', jsonb_build_object(
    'workspaces', (SELECT count(*) FROM workspaces),
    'users', (SELECT count(*) FROM users),
    'workspace_members', (SELECT count(*) FROM workspace_members),
    'accounts_total', (SELECT count(*) FROM accounts),
    'accounts_active', (SELECT count(*) FROM accounts WHERE deleted_at IS NULL),
    'categories_total', (SELECT count(*) FROM categories),
    'categories_active', (SELECT count(*) FROM categories WHERE deleted_at IS NULL),
    'payees', (SELECT count(*) FROM payees),
    'transactions_total', (SELECT count(*) FROM transactions),
    'transactions_active', (SELECT count(*) FROM transactions WHERE deleted_at IS NULL),
    'transaction_splits', (SELECT count(*) FROM transaction_splits),
    'budget_periods', (SELECT count(*) FROM budget_periods),
    'budget_allocations', (SELECT count(*) FROM budget_allocations),
    'goals', (SELECT count(*) FROM goals),
    'recurring_rules', (SELECT count(*) FROM recurring_rules),
    'import_batches', (SELECT count(*) FROM import_batches),
    'month_closures', (SELECT count(*) FROM month_closures),
    'google_sheet_bindings', (SELECT count(*) FROM google_sheet_bindings),
    'google_connections', (SELECT count(*) FROM google_connections),
    'latest_transaction_occurred_at', (
      SELECT to_char(max(occurred_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
      FROM transactions
    )
  ),
  -- Recorded but never compared. The backup pipeline itself appends backup.created,
  -- backup.verified and restore.verified rows to the SOURCE after the dump was taken, so these
  -- counts legitimately differ on both sides of a restore.
  'informational', jsonb_build_object(
    'audit_log', (SELECT count(*) FROM audit_log),
    'auth_sessions', (SELECT count(*) FROM auth_sessions),
    'sync_outbox', (SELECT count(*) FROM sync_outbox),
    'sync_inbox', (SELECT count(*) FROM sync_inbox)
  )
));
COMMIT;
SQL
