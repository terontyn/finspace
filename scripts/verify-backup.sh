#!/bin/sh
set -eu

backup_dir="${BACKUP_STORAGE_PATH:-/backups/database}"
case "$backup_dir" in
  /backups|/backups/*) ;;
  *) echo "Verification refused: BACKUP_STORAGE_PATH must stay inside /backups." >&2; exit 1 ;;
esac
create_backup="false"
if [ "${1:-}" = "--create" ]; then
  create_backup="true"
  shift
fi
[ "$#" -le 1 ] || { echo "Usage: verify-backup.sh [--create] [DUMP_FILE]" >&2; exit 2; }
requested_dump="${1:-}"
[ "$create_backup" = "false" ] || [ -z "$requested_dump" ] || {
  echo "Usage: verify-backup.sh [--create] [DUMP_FILE]" >&2
  exit 2
}

if [ "$create_backup" = "true" ]; then
  dump_path="$(sh /scripts/backup.sh | tail -n 1)"
elif [ -n "$requested_dump" ]; then
  # Verifying one named dump instead of the newest one: a backup set must prove the exact
  # artifact it references, not whatever happens to sort last.
  case "$requested_dump" in
    "$backup_dir"/finspace_*.dump) ;;
    *) echo "Backup verification refused: dump path is outside the backup directory." >&2; exit 1 ;;
  esac
  dump_path="$requested_dump"
else
  dump_path="$(find "$backup_dir" -maxdepth 1 -type f -name 'finspace_*.dump' -printf '%p\n' | sort -r | head -n 1)"
fi
[ -n "$dump_path" ] && [ -s "$dump_path" ] || { echo "Backup verification failed: no non-empty dump found." >&2; exit 1; }

manifest_path="${dump_path}.manifest.json"
[ -s "$manifest_path" ] || { echo "Backup verification failed: manifest is missing." >&2; exit 1; }
pg_restore --list "$dump_path" >/dev/null

expected_sha="$(sed -n 's/.*"sha256": "\([0-9a-f][0-9a-f]*\)".*/\1/p' "$manifest_path")"
actual_sha="$(sha256sum "$dump_path" | awk '{print $1}')"
[ ${#expected_sha} -eq 64 ] && [ "$expected_sha" = "$actual_sha" ] || {
  echo "Backup verification failed: SHA-256 mismatch." >&2
  exit 1
}

psql -XAtq -v manifest="$(tr -d '\n' <"$manifest_path")" <<'SQL' >/dev/null
SELECT :'manifest'::jsonb IS NOT NULL;
SQL

source_revision="$(psql -XAtq -c "SELECT version_num FROM alembic_version LIMIT 1")"
restore_database="finspace_restore_test_$(date -u +%Y%m%d%H%M%S)_$$"
cleanup_restore() {
  dropdb --if-exists "$restore_database" >/dev/null 2>&1 || true
}
trap cleanup_restore EXIT HUP INT TERM

sh /scripts/restore.sh "$dump_path" "$restore_database" >/dev/null
restored_revision="$(psql -XAtq -d "$restore_database" -c "SELECT version_num FROM alembic_version LIMIT 1")"
[ "$restored_revision" = "$source_revision" ] || {
  echo "Backup verification failed: Alembic revision mismatch." >&2
  exit 1
}

for table in users workspaces workspace_members accounts categories transactions audit_log auth_sessions import_batches import_rows system_metadata google_connections google_oauth_flows google_sheet_bindings sync_outbox sync_inbox sync_conflicts sync_runs service_accounts service_api_keys automation_runs recurring_rules recurring_rule_executions telegram_links telegram_link_codes telegram_intents month_closures notification_settings; do
  exists="$(psql -XAtq -d "$restore_database" -v table="$table" <<'SQL'
SELECT to_regclass('public.' || :'table') IS NOT NULL;
SQL
)"
  [ "$exists" = "t" ] || { echo "Backup verification failed: table $table is missing." >&2; exit 1; }
done
for column in provider binding_secret_hash binding_secret_created_at last_heartbeat_at last_pull_at last_ack_at; do
  exists="$(psql -XAtq -d "$restore_database" -v column="$column" <<'SQL'
SELECT EXISTS (
  SELECT 1
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name = 'google_sheet_bindings'
    AND column_name = :'column'
);
SQL
)"
  [ "$exists" = "t" ] || {
    echo "Backup verification failed: google_sheet_bindings.$column is missing." >&2
    exit 1
  }
done
psql -XAtq -d "$restore_database" -c "BEGIN READ ONLY; SELECT count(*) FROM system_metadata; COMMIT" >/dev/null

psql -Xq -v filename="$(basename "$dump_path")" -v sha256="$actual_sha" -v restored_database="$restore_database" <<'SQL'
INSERT INTO audit_log (
  id, workspace_id, actor_user_id, entity_type, entity_id, action,
  before_data, after_data, request_id, source
)
VALUES
  (gen_random_uuid(), NULL, NULL, 'backup', gen_random_uuid(), 'backup.verified', NULL,
   jsonb_build_object('filename', :'filename', 'sha256', :'sha256'), NULL, 'system'),
  (gen_random_uuid(), NULL, NULL, 'restore', gen_random_uuid(), 'restore.verified', NULL,
   jsonb_build_object('filename', :'filename', 'target', :'restored_database'), NULL, 'system');
SQL

cleanup_restore
trap - EXIT HUP INT TERM
if [ "${BACKUP_REMOTE_AFTER_VERIFY:-true}" = "true" ]; then
  sh /scripts/backup-secondary-copy.sh "$dump_path"
fi
if [ "$create_backup" = "true" ]; then
  sh /scripts/backup-cleanup.sh >/dev/null
fi
masked_sha="$(printf '%s' "$actual_sha" | cut -c1-12)..."
echo "Backup verified: $(basename "$dump_path") (sha256=$masked_sha, revision=$restored_revision)."
