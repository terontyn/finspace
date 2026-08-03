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

psql -Xq -v filename="$filename" -v sha256="$sha256" -v size_bytes="$size_bytes" <<'SQL'
INSERT INTO audit_log (
  id, workspace_id, actor_user_id, entity_type, entity_id, action,
  before_data, after_data, request_id, source
)
VALUES (
  gen_random_uuid(), NULL, NULL, 'backup', gen_random_uuid(), 'backup.created',
  NULL,
  jsonb_build_object('filename', :'filename', 'sha256', :'sha256', 'size_bytes', :'size_bytes'::bigint),
  NULL, 'system'
);
SQL

trap - EXIT HUP INT TERM
echo "$dump_path"
