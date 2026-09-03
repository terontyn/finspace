#!/bin/sh
set -eu

umask 077

backup_dir="${BACKUP_STORAGE_PATH:-/backups/database}"
case "$backup_dir" in
  /backups|/backups/*) ;;
  *) echo "Backup failed: BACKUP_STORAGE_PATH must stay inside /backups." >&2; exit 1 ;;
esac
database="${PGDATABASE:?PGDATABASE is required}"
timestamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
filename="finspace_${timestamp}.dump"
dump_path="${backup_dir}/${filename}"
partial_path="${dump_path}.partial"
manifest_path="${dump_path}.manifest.json"

cleanup_partial() {
  rm -f -- "$partial_path" "${manifest_path}.partial"
}
trap cleanup_partial EXIT HUP INT TERM

mkdir -p "$backup_dir"
chmod 700 "$backup_dir" 2>/dev/null || true
[ ! -e "$dump_path" ] && [ ! -e "$manifest_path" ] || {
  echo "Backup failed: timestamp collision for $filename." >&2
  exit 1
}

pg_isready -q
alembic_revision="$(psql -XAtq -c "SELECT version_num FROM alembic_version LIMIT 1")"
if [ -z "$alembic_revision" ]; then
  echo "Backup failed: the Alembic revision is unavailable." >&2
  exit 1
fi

pg_dump --format=custom --no-owner --no-privileges --file="$partial_path" "$database"
if [ ! -s "$partial_path" ]; then
  echo "Backup failed: pg_dump produced an empty file." >&2
  exit 1
fi
pg_restore --list "$partial_path" >/dev/null

sha256="$(sha256sum "$partial_path" | awk '{print $1}')"
size_bytes="$(stat -c %s "$partial_path")"
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mv "$partial_path" "$dump_path"
printf '{\n  "filename": "%s",\n  "sha256": "%s",\n  "created_at": "%s",\n  "database": "%s",\n  "alembic_revision": "%s",\n  "format": "postgresql-custom",\n  "size_bytes": %s\n}\n' \
  "$filename" "$sha256" "$created_at" "$database" "$alembic_revision" "$size_bytes" \
  >"${manifest_path}.partial"
mv "${manifest_path}.partial" "$manifest_path"

# The revision travels with the audit event as well as in the manifest. The manifest stays part of
# the restore artifact and remains authoritative there, but it lives in a 0700 root-owned directory
# the non-root backend cannot read; the audit row is the operational read model. The value is the
# one already captured above — no second query against alembic_version.
psql -Xq   -v filename="$filename"   -v sha256="$sha256"   -v size_bytes="$size_bytes"   -v alembic_revision="$alembic_revision" <<'SQL'
INSERT INTO audit_log (
  id, workspace_id, actor_user_id, entity_type, entity_id, action,
  before_data, after_data, request_id, source
)
VALUES (
  gen_random_uuid(), NULL, NULL, 'backup', gen_random_uuid(), 'backup.created',
  NULL,
  jsonb_build_object(
    'filename', :'filename',
    'sha256', :'sha256',
    'size_bytes', :'size_bytes'::bigint,
    'alembic_revision', :'alembic_revision'
  ),
  NULL, 'system'
);
SQL

# Deterministic handoff for unattended callers: the scheduler must know exactly which dump this
# run created, never "whichever file sorts last" — a concurrent or manual run would make that
# ambiguous. The basename is written atomically and only inside the backup tree.
if [ -n "${BACKUP_RESULT_FILE:-}" ]; then
  case "$BACKUP_RESULT_FILE" in
    /backups/*) ;;
    *) echo "Backup failed: BACKUP_RESULT_FILE must stay inside /backups." >&2; exit 1 ;;
  esac
  case "$BACKUP_RESULT_FILE" in
    *..*) echo "Backup failed: BACKUP_RESULT_FILE must not traverse directories." >&2; exit 1 ;;
  esac
  printf '%s
' "$filename" >"${BACKUP_RESULT_FILE}.partial"
  mv "${BACKUP_RESULT_FILE}.partial" "$BACKUP_RESULT_FILE"
fi

trap - EXIT HUP INT TERM
echo "$dump_path"
