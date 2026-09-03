#!/bin/sh
# Update the mutable run evidence of a backup set.
#
# backup-set.json is the immutable inventory and is never rewritten. This touches only
# backup-set-report.json, so the record of what a run produced stays separate from the record of
# what has been verified about it.
set -eu

umask 077

usage() {
  echo "Usage: backup-set-report.sh SET_ID --offhost-verified LABEL" >&2
  echo "       backup-set-report.sh SET_ID --offhost-failed REASON" >&2
}

[ "$#" -eq 3 ] || { usage; exit 2; }
set_id="$1"
mode="$2"
value="$3"

case "$set_id" in
  ????-??-??T??????Z) ;;
  *) echo "Report update refused: unsafe set id." >&2; exit 1 ;;
esac
case "$set_id" in
  *[!0-9TZ-]*) echo "Report update refused: unsafe set id." >&2; exit 1 ;;
esac
# Never let a free-form failure reason carry a secret or break the JSON.
case "$value" in
  ''|*[!A-Za-z0-9._\ -]*) echo "Report update refused: unsafe value." >&2; exit 1 ;;
esac

backup_root="${FINSPACE_BACKUP_ROOT:-/opt/finspace/backups}"
report="${backup_root}/sets/${set_id}/backup-set-report.json"
[ -s "$report" ] || { echo "Report update failed: report is missing." >&2; exit 1; }

json_string() {
  sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" "$report" | head -n 1
}
json_bool() {
  sed -n "s/.*\"$1\": *\\([a-z]*\\).*/\\1/p" "$report" | head -n 1
}

created_at="$(json_string created_at)"
local_verified="$(json_bool local_verified)"
local_verified_at="$(json_string local_verified_at)"
[ -n "$local_verified" ] || { echo "Report update failed: report is malformed." >&2; exit 1; }
if [ -n "$local_verified_at" ]; then
  local_verified_at_json="\"$local_verified_at\""
else
  local_verified_at_json="null"
fi

now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
case "$mode" in
  --offhost-verified)
    offhost="true"
    offhost_at="\"$now\""
    label="\"$value\""
    error="null"
    ;;
  --offhost-failed)
    offhost="false"
    offhost_at="null"
    label="null"
    error="\"$value\""
    ;;
  *) usage; exit 2 ;;
esac

partial="${report}.partial"
cleanup_partial() {
  rm -f -- "$partial"
}
trap cleanup_partial EXIT HUP INT TERM

printf '{\n  "version": 1,\n  "set_id": "%s",\n  "created_at": "%s",\n  "local_verified": %s,\n  "local_verified_at": %s,\n  "offhost_verified": %s,\n  "offhost_verified_at": %s,\n  "offhost_destination_label": %s,\n  "error": %s\n}\n' \
  "$set_id" "$created_at" "$local_verified" "$local_verified_at_json" \
  "$offhost" "$offhost_at" "$label" "$error" \
  >"$partial"

mv "$partial" "$report"
chmod 600 "$report" 2>/dev/null || true
trap - EXIT HUP INT TERM
