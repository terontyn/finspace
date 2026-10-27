#!/bin/sh
set -eu

umask 077

[ "${BACKUP_REMOTE_PROVIDER:-disabled}" = "local_secondary_path" ] || {
  echo "Secondary copy skipped: provider is disabled."
  exit 0
}

backup_dir="${BACKUP_STORAGE_PATH:-/backups/database}"
secondary_dir="${BACKUP_SECONDARY_PATH:-/secondary}"
case "$backup_dir" in
  /backups|/backups/*) ;;
  *) echo "Secondary copy refused: primary path must stay inside /backups." >&2; exit 1 ;;
esac
case "$secondary_dir" in
  /secondary|/secondary/*) ;;
  *) echo "Secondary copy refused: target path must stay inside /secondary." >&2; exit 1 ;;
esac

dump_path="${1:-}"
[ -n "$dump_path" ] || dump_path="$(find "$backup_dir" -maxdepth 1 -type f -name 'finspace_*.dump' -printf '%p\n' | sort -r | head -n 1)"
case "$dump_path" in
  "$backup_dir"/finspace_*.dump) ;;
  *) echo "Secondary copy refused: dump path is outside primary backup directory." >&2; exit 1 ;;
esac
[ -s "$dump_path" ] || { echo "Secondary copy failed: dump is missing." >&2; exit 1; }
manifest_path="${dump_path}.manifest.json"
[ -s "$manifest_path" ] || { echo "Secondary copy failed: manifest is missing." >&2; exit 1; }

expected_sha="$(sed -n 's/.*"sha256": "\([0-9a-f][0-9a-f]*\)".*/\1/p' "$manifest_path")"
actual_sha="$(sha256sum "$dump_path" | awk '{print $1}')"
[ ${#expected_sha} -eq 64 ] && [ "$expected_sha" = "$actual_sha" ] || {
  echo "Secondary copy failed: SHA-256 mismatch." >&2
  exit 1
}
pg_restore --list "$dump_path" >/dev/null

mkdir -p "$secondary_dir"
dump_name="$(basename "$dump_path")"
manifest_name="$(basename "$manifest_path")"
dump_partial="${secondary_dir}/${dump_name}.partial"
manifest_partial="${secondary_dir}/${manifest_name}.partial"
cleanup_partial() {
  rm -f -- "$dump_partial" "$manifest_partial"
}
trap cleanup_partial EXIT HUP INT TERM
cp "$dump_path" "$dump_partial"
cp "$manifest_path" "$manifest_partial"
mv "$dump_partial" "${secondary_dir}/${dump_name}"
mv "$manifest_partial" "${secondary_dir}/${manifest_name}"

psql -Xq -v filename="$dump_name" -v sha256="$actual_sha" <<'SQL'
INSERT INTO audit_log (
  id, workspace_id, actor_user_id, entity_type, entity_id, action,
  before_data, after_data, request_id, source
)
VALUES (
  gen_random_uuid(), NULL, NULL, 'backup', gen_random_uuid(), 'backup.remote.copy',
  NULL, jsonb_build_object('filename', :'filename', 'sha256', :'sha256'), NULL, 'system'
);
SQL

trap - EXIT HUP INT TERM
echo "Verified backup copied to configured secondary storage: $dump_name"
