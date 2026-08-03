#!/bin/sh
set -eu

usage() {
  echo "Usage: restore.sh EXACT_DUMP_FILE [TARGET_DATABASE] [--overwrite-main]" >&2
}

[ "$#" -ge 1 ] || { usage; exit 2; }
dump_path="$1"
shift
case "$dump_path" in
  /backups/*) ;;
  *) echo "Restore refused: dump path must stay inside /backups." >&2; exit 1 ;;
esac
target_database="finspace_restore_test"
overwrite_main="false"

if [ "$#" -gt 0 ] && [ "$1" != "--overwrite-main" ]; then
  target_database="$1"
  shift
fi
if [ "$#" -gt 0 ] && [ "$1" = "--overwrite-main" ]; then
  overwrite_main="true"
  shift
fi
[ "$#" -eq 0 ] || { usage; exit 2; }

[ -f "$dump_path" ] || { echo "Restore failed: exact dump file does not exist: $dump_path" >&2; exit 1; }
case "$target_database" in
  ''|*[!A-Za-z0-9_]*) echo "Restore failed: unsafe target database name." >&2; exit 1 ;;
esac
pg_restore --list "$dump_path" >/dev/null

main_database="${POSTGRES_DB:-${PGDATABASE:?PGDATABASE is required}}"
if [ "$target_database" = "$main_database" ]; then
  if [ "$overwrite_main" != "true" ]; then
    echo "Restore refused: overwriting the working database requires --overwrite-main." >&2
    exit 1
  fi
  expected="OVERWRITE ${main_database}"
  if [ "${RESTORE_CONFIRMATION:-}" != "$expected" ]; then
    echo "Type exactly: $expected" >&2
    IFS= read -r confirmation
  else
    confirmation="$RESTORE_CONFIRMATION"
  fi
  [ "$confirmation" = "$expected" ] || { echo "Restore cancelled." >&2; exit 1; }
else
  dropdb --if-exists "$target_database"
  createdb "$target_database"
fi

pg_restore --exit-on-error --clean --if-exists --no-owner --no-privileges --dbname="$target_database" "$dump_path"

revision="$(psql -XAtq -d "$target_database" -c "SELECT version_num FROM alembic_version LIMIT 1")"
table_count="$(psql -XAtq -d "$target_database" -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
metadata_table="$(psql -XAtq -d "$target_database" -c "SELECT to_regclass('public.system_metadata') IS NOT NULL")"
psql -XAtq -d "$target_database" -c "SELECT count(*) FROM system_metadata" >/dev/null

[ -n "$revision" ] || { echo "Restore failed: missing Alembic revision." >&2; exit 1; }
[ "$table_count" -ge 10 ] || { echo "Restore failed: expected core tables are missing." >&2; exit 1; }
[ "$metadata_table" = "t" ] || { echo "Restore failed: system_metadata is missing." >&2; exit 1; }

echo "Restored $dump_path to $target_database (revision=$revision, tables=$table_count)."
