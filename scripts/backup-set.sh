#!/bin/sh
# Group the artifacts of one backup run into a backup set.
#
# The PostgreSQL dump stays canonical in /backups/database: the backend reads its manifest from
# BACKUP_METADATA_PATH, and a large dump must not be duplicated locally. The set is a manifest that
# references it, so a restore can tell which artifacts belong together.
#
# Two files are written, and they are deliberately different kinds of thing:
#   backup-set.json         immutable inventory of what the run produced
#   backup-set-report.json  mutable evidence of what has been verified so far
#
# Consistency: the PostgreSQL dump is one transactional snapshot and is the financial source of
# truth. An optional n8n archive is a later cold snapshot of an optional integration. There is no
# cross-service atomic instant and none is claimed.
set -eu

umask 077

usage() {
  echo "Usage: backup-set.sh DUMP_FILE" >&2
  echo "  FINSPACE_COMMIT must be set; FINSPACE_TAG is optional." >&2
}

backup_root="/backups"
backup_dir="${BACKUP_STORAGE_PATH:-/backups/database}"
case "$backup_dir" in
  /backups|/backups/*) ;;
  *) echo "Backup set refused: BACKUP_STORAGE_PATH must stay inside /backups." >&2; exit 1 ;;
esac

[ "$#" -eq 1 ] || { usage; exit 2; }
dump_path="$1"
case "$dump_path" in
  "$backup_dir"/finspace_*.dump) ;;
  *) echo "Backup set refused: dump path is outside the primary backup directory." >&2; exit 1 ;;
esac
case "$dump_path" in
  *..*) echo "Backup set refused: dump path must not traverse directories." >&2; exit 1 ;;
esac
[ -s "$dump_path" ] || { echo "Backup set failed: dump is missing or empty." >&2; exit 1; }

manifest_path="${dump_path}.manifest.json"
[ -s "$manifest_path" ] || { echo "Backup set failed: database manifest is missing." >&2; exit 1; }

dump_name="$(basename "$dump_path")"
set_id="$(printf '%s' "$dump_name" | sed -n 's/^finspace_\(.*\)\.dump$/\1/p')"
# Conservative and derived, never operator input: one dump maps to exactly one set.
case "$set_id" in
  ????-??-??T??????Z) ;;
  *) echo "Backup set refused: unsafe set id derived from $dump_name." >&2; exit 1 ;;
esac
case "$set_id" in
  *[!0-9TZ-]*) echo "Backup set refused: unsafe set id derived from $dump_name." >&2; exit 1 ;;
esac

commit="${FINSPACE_COMMIT:-}"
case "$commit" in
  '') echo "Backup set failed: FINSPACE_COMMIT is required." >&2; exit 1 ;;
  *[!0-9a-f]*) echo "Backup set failed: FINSPACE_COMMIT must be a hexadecimal commit." >&2; exit 1 ;;
esac
[ ${#commit} -ge 7 ] || { echo "Backup set failed: FINSPACE_COMMIT is too short." >&2; exit 1; }
tag="${FINSPACE_TAG:-}"
case "$tag" in
  *[!A-Za-z0-9._-]*) echo "Backup set failed: FINSPACE_TAG contains unsafe characters." >&2; exit 1 ;;
esac

json_field() {
  sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" "$2" | head -n 1
}

expected_sha="$(json_field sha256 "$manifest_path")"
alembic_revision="$(json_field alembic_revision "$manifest_path")"
[ ${#expected_sha} -eq 64 ] || { echo "Backup set failed: manifest SHA-256 is malformed." >&2; exit 1; }
case "$expected_sha" in
  *[!0-9a-f]*) echo "Backup set failed: manifest SHA-256 is malformed." >&2; exit 1 ;;
esac
[ -n "$alembic_revision" ] || { echo "Backup set failed: manifest has no Alembic revision." >&2; exit 1; }

actual_sha="$(sha256sum "$dump_path" | awk '{print $1}')"
[ "$expected_sha" = "$actual_sha" ] || {
  echo "Backup set failed: dump SHA-256 does not match its manifest." >&2
  exit 1
}
pg_restore --list "$dump_path" >/dev/null

dump_size="$(stat -c %s "$dump_path")"
manifest_size="$(stat -c %s "$manifest_path")"
manifest_sha="$(sha256sum "$manifest_path" | awk '{print $1}')"

set_dir="${backup_root}/sets/${set_id}"
set_manifest="${set_dir}/backup-set.json"
set_report="${set_dir}/backup-set-report.json"
mkdir -p "$set_dir"
chmod 700 "${backup_root}/sets" "$set_dir" 2>/dev/null || true

# An optional cold n8n archive is produced before this script runs, into the same set directory.
n8n_archive="${set_dir}/n8n-data.tar.gz"
n8n_included="false"
n8n_sha=""
n8n_size="0"
if [ -s "$n8n_archive" ]; then
  n8n_included="true"
  n8n_sha="$(sha256sum "$n8n_archive" | awk '{print $1}')"
  n8n_size="$(stat -c %s "$n8n_archive")"
  recorded_sha=""
  [ -s "${set_dir}/n8n-data.sha256" ] &&
    recorded_sha="$(awk '{print $1}' "${set_dir}/n8n-data.sha256")"
  [ -z "$recorded_sha" ] || [ "$recorded_sha" = "$n8n_sha" ] || {
    echo "Backup set failed: n8n archive SHA-256 does not match its recorded digest." >&2
    exit 1
  }
fi

if [ -e "$set_manifest" ]; then
  # Re-running against an identical inventory is harmless; conflicting content is not.
  existing_sha="$(json_field sha256 "$set_manifest")"
  [ "$existing_sha" = "$actual_sha" ] || {
    echo "Backup set refused: $set_id already exists with a different database dump." >&2
    exit 1
  }
fi

cleanup_partial() {
  rm -f -- "${set_manifest}.partial" "${set_report}.partial"
}
trap cleanup_partial EXIT HUP INT TERM

if [ "$n8n_included" = "true" ]; then
  n8n_json="{\n      \"included\": true,\n      \"path\": \"sets/${set_id}/n8n-data.tar.gz\",\n      \"sha256\": \"${n8n_sha}\",\n      \"size_bytes\": ${n8n_size}\n    }"
else
  n8n_json="{\n      \"included\": false,\n      \"path\": null,\n      \"sha256\": null,\n      \"size_bytes\": null\n    }"
fi
if [ -n "$tag" ]; then
  tag_json="\"${tag}\""
else
  tag_json="null"
fi

created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# shellcheck disable=SC2059
printf "{\n  \"version\": 1,\n  \"set_id\": \"%s\",\n  \"created_at\": \"%s\",\n  \"finspace_commit\": \"%s\",\n  \"finspace_tag\": %s,\n  \"alembic_revision\": \"%s\",\n  \"database\": {\n      \"path\": \"database/%s\",\n      \"manifest_path\": \"database/%s.manifest.json\",\n      \"filename\": \"%s\",\n      \"sha256\": \"%s\",\n      \"manifest_sha256\": \"%s\",\n      \"size_bytes\": %s,\n      \"manifest_size_bytes\": %s\n    },\n  \"n8n\": ${n8n_json}\n}\n" \
  "$set_id" "$created_at" "$commit" "$tag_json" "$alembic_revision" \
  "$dump_name" "$dump_name" "$dump_name" "$actual_sha" "$manifest_sha" \
  "$dump_size" "$manifest_size" \
  >"${set_manifest}.partial"

# The database verification contract is not re-implemented here: SHA equality alone is not proof.
# verify-backup.sh restores the dump into a throwaway database and checks the Alembic revision,
# every required table and every required column. The secondary copy is suppressed because the
# off-host step is a separate, host-level stage.
local_verified="false"
verify_error="null"
if BACKUP_REMOTE_AFTER_VERIFY=false sh /scripts/verify-backup.sh "$dump_path" >/dev/null 2>&1; then
  local_verified="true"
  verified_at="\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
else
  verified_at="null"
  verify_error="\"database verification failed\""
fi

# shellcheck disable=SC2059
printf "{\n  \"version\": 1,\n  \"set_id\": \"%s\",\n  \"created_at\": \"%s\",\n  \"local_verified\": %s,\n  \"local_verified_at\": %s,\n  \"offhost_verified\": false,\n  \"offhost_verified_at\": null,\n  \"offhost_destination_label\": null,\n  \"error\": %s\n}\n" \
  "$set_id" "$created_at" "$local_verified" "$verified_at" "$verify_error" \
  >"${set_report}.partial"

mv "${set_manifest}.partial" "$set_manifest"
mv "${set_report}.partial" "$set_report"
chmod 600 "$set_manifest" "$set_report" 2>/dev/null || true
trap - EXIT HUP INT TERM

[ "$local_verified" = "true" ] || {
  echo "Backup set $set_id written, but database verification FAILED." >&2
  exit 1
}
echo "Backup set $set_id ready (revision=$alembic_revision, n8n=$n8n_included)."
echo "$set_dir"
