#!/bin/sh
# Copy one verified backup set to a genuinely separate host over SSH.
#
# This runs on the HOST, never inside an application container: the SSH key and the remote address
# are host-only configuration and must never enter the backend, frontend or worker environments.
#
# A second directory on the same disk is not a second failure domain. `backup-secondary-copy.sh`
# remains useful where /secondary is another physical device; this script is what the v1.0 off-host
# guarantee rests on.
#
# The remote set is self-contained even though the canonical dump stays in /backups/database.
set -eu

umask 077

usage() {
  echo "Usage: backup-offhost.sh SET_ID" >&2
  echo "  Required: FINSPACE_BACKUP_REMOTE_HOST, FINSPACE_BACKUP_REMOTE_USER," >&2
  echo "            FINSPACE_BACKUP_REMOTE_ROOT, FINSPACE_BACKUP_SSH_KEY," >&2
  echo "            FINSPACE_BACKUP_KNOWN_HOSTS" >&2
  echo "  Optional: FINSPACE_BACKUP_REMOTE_LABEL, FINSPACE_BACKUP_ROOT" >&2
}

[ "$#" -eq 1 ] || { usage; exit 2; }
set_id="$1"
case "$set_id" in
  ????-??-??T??????Z) ;;
  *) echo "Off-host copy refused: unsafe set id." >&2; exit 1 ;;
esac
case "$set_id" in
  *[!0-9TZ-]*) echo "Off-host copy refused: unsafe set id." >&2; exit 1 ;;
esac

backup_root="${FINSPACE_BACKUP_ROOT:-/opt/finspace/backups}"
[ -d "$backup_root" ] || { echo "Off-host copy failed: backup root does not exist." >&2; exit 1; }

require() {
  eval "value=\${$1:-}"
  [ -n "$value" ] || { echo "Off-host copy refused: $1 is required." >&2; exit 1; }
}
require FINSPACE_BACKUP_REMOTE_HOST
require FINSPACE_BACKUP_REMOTE_USER
require FINSPACE_BACKUP_REMOTE_ROOT
require FINSPACE_BACKUP_SSH_KEY
require FINSPACE_BACKUP_KNOWN_HOSTS

remote_host="$FINSPACE_BACKUP_REMOTE_HOST"
remote_user="$FINSPACE_BACKUP_REMOTE_USER"
remote_root="$FINSPACE_BACKUP_REMOTE_ROOT"
ssh_key="$FINSPACE_BACKUP_SSH_KEY"
known_hosts="$FINSPACE_BACKUP_KNOWN_HOSTS"
label="${FINSPACE_BACKUP_REMOTE_LABEL:-offhost}"
case "$label" in
  *[!A-Za-z0-9._-]*) echo "Off-host copy refused: unsafe destination label." >&2; exit 1 ;;
esac
case "$remote_root" in
  /*) ;;
  *) echo "Off-host copy refused: FINSPACE_BACKUP_REMOTE_ROOT must be absolute." >&2; exit 1 ;;
esac
# Allowlist rather than blocklist: these values are interpolated into a remote shell command.
case "$remote_root" in *[!A-Za-z0-9._/-]*)
  echo "Off-host copy refused: FINSPACE_BACKUP_REMOTE_ROOT contains unsafe characters." >&2
  exit 1 ;;
esac
case "$remote_user" in *[!A-Za-z0-9._-]*)
  echo "Off-host copy refused: FINSPACE_BACKUP_REMOTE_USER contains unsafe characters." >&2
  exit 1 ;;
esac
case "$remote_host" in *[!A-Za-z0-9._:-]*)
  echo "Off-host copy refused: FINSPACE_BACKUP_REMOTE_HOST contains unsafe characters." >&2
  exit 1 ;;
esac

# Fail before any network command if the credentials are unusable or unpinned.
[ -f "$ssh_key" ] || { echo "Off-host copy refused: SSH key does not exist." >&2; exit 1; }
[ -s "$known_hosts" ] || {
  echo "Off-host copy refused: known_hosts is missing or empty; the host key must be pinned." >&2
  exit 1
}
key_mode="$(stat -c %a "$ssh_key" 2>/dev/null || echo "")"
case "$key_mode" in
  600|400|0600|0400) ;;
  '') echo "Off-host copy refused: SSH key permissions could not be determined." >&2; exit 1 ;;
  *) echo "Off-host copy refused: SSH key must not be group- or world-readable." >&2; exit 1 ;;
esac

set_dir="${backup_root}/sets/${set_id}"
manifest="${set_dir}/backup-set.json"
report="${set_dir}/backup-set-report.json"
[ -s "$manifest" ] || { echo "Off-host copy failed: backup-set.json is missing." >&2; exit 1; }
[ -s "$report" ] || { echo "Off-host copy failed: backup-set-report.json is missing." >&2; exit 1; }

json_field() {
  sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" "$2" | head -n 1
}

local_verified="$(sed -n 's/.*"local_verified": *\([a-z]*\).*/\1/p' "$report" | head -n 1)"
[ "$local_verified" = "true" ] || {
  echo "Off-host copy refused: the set has not passed local database verification." >&2
  exit 1
}

dump_relative="$(json_field path "$manifest")"
dump_sha="$(json_field sha256 "$manifest")"
manifest_sha="$(json_field manifest_sha256 "$manifest")"
case "$dump_relative" in
  database/finspace_*.dump) ;;
  *) echo "Off-host copy refused: unexpected database artifact path." >&2; exit 1 ;;
esac
case "$dump_relative" in
  *..*) echo "Off-host copy refused: artifact path traverses directories." >&2; exit 1 ;;
esac
[ ${#dump_sha} -eq 64 ] || { echo "Off-host copy refused: malformed database SHA-256." >&2; exit 1; }

dump_path="${backup_root}/${dump_relative}"
dump_manifest_path="${dump_path}.manifest.json"
[ -s "$dump_path" ] || { echo "Off-host copy failed: referenced dump is missing." >&2; exit 1; }
[ -s "$dump_manifest_path" ] || {
  echo "Off-host copy failed: referenced database manifest is missing." >&2
  exit 1
}

verify_sha() {
  actual="$(sha256sum "$1" | awk '{print $1}')"
  [ "$actual" = "$2" ] || {
    echo "Off-host copy failed: local SHA-256 mismatch for $(basename "$1")." >&2
    exit 1
  }
}
verify_sha "$dump_path" "$dump_sha"
[ -z "$manifest_sha" ] || verify_sha "$dump_manifest_path" "$manifest_sha"

n8n_included="$(sed -n 's/.*"included": *\([a-z]*\).*/\1/p' "$manifest" | head -n 1)"
n8n_archive="${set_dir}/n8n-data.tar.gz"
if [ "$n8n_included" = "true" ]; then
  [ -s "$n8n_archive" ] || { echo "Off-host copy failed: n8n archive is missing." >&2; exit 1; }
  [ -s "${set_dir}/n8n-data.sha256" ] || {
    echo "Off-host copy failed: n8n archive digest is missing." >&2
    exit 1
  }
  n8n_sha="$(awk '{print $1}' "${set_dir}/n8n-data.sha256")"
  case "$n8n_sha" in
    *[!0-9a-f]*) echo "Off-host copy refused: malformed n8n SHA-256." >&2; exit 1 ;;
  esac
  [ ${#n8n_sha} -eq 64 ] || { echo "Off-host copy refused: malformed n8n SHA-256." >&2; exit 1; }
  verify_sha "$n8n_archive" "$n8n_sha"
fi

# The upload directory is what makes the remote set self-contained; it is staged with hard links
# where possible so a large dump is not duplicated on disk.
staging="$(mktemp -d "${TMPDIR:-/tmp}/finspace-offhost-XXXXXX")"
cleanup_staging() {
  rm -rf -- "$staging"
}
trap cleanup_staging EXIT HUP INT TERM
chmod 700 "$staging"

link_or_copy() {
  ln "$1" "$2" 2>/dev/null || cp "$1" "$2"
}
link_or_copy "$dump_path" "$staging/database.dump"
link_or_copy "$dump_manifest_path" "$staging/database.manifest.json"
cp "$manifest" "$staging/backup-set.json"
cp "$report" "$staging/backup-set-report.json"
if [ "$n8n_included" = "true" ]; then
  link_or_copy "$n8n_archive" "$staging/n8n-data.tar.gz"
  [ ! -s "${set_dir}/n8n-data.sha256" ] || cp "${set_dir}/n8n-data.sha256" "$staging/n8n-data.sha256"
fi
# Plain sha256sum output so the remote needs nothing beyond a POSIX shell and coreutils.
( cd "$staging" && sha256sum -- * >SHA256SUMS )
chmod 600 "$staging"/* 2>/dev/null || true

ssh_options="-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$known_hosts -o IdentitiesOnly=yes -i $ssh_key"
remote="${remote_user}@${remote_host}"
remote_sets="${remote_root}/finspace/sets"
remote_partial="${remote_sets}/.${set_id}.partial"
remote_final="${remote_sets}/${set_id}"

# shellcheck disable=SC2086
run_ssh() {
  ssh $ssh_options "$remote" "$@"
}

# Refuse rather than overwrite: an existing final set is evidence of an earlier successful run.
if run_ssh "test -e '$remote_final'"; then
  echo "Off-host copy refused: $set_id already exists on the remote." >&2
  exit 1
fi
run_ssh "umask 077; rm -rf -- '$remote_partial'; mkdir -p '$remote_partial'"

# shellcheck disable=SC2086
rsync --archive --checksum --chmod=D700,F600 \
  -e "ssh $ssh_options" "$staging/" "${remote}:${remote_partial}/"

if ! run_ssh "cd '$remote_partial' && sha256sum -c SHA256SUMS >/dev/null 2>&1"; then
  echo "Off-host copy failed: remote SHA-256 verification did not pass." >&2
  run_ssh "rm -rf -- '$remote_partial'" || true
  FINSPACE_BACKUP_ROOT="$backup_root"     sh "$(cd "$(dirname "$0")" && pwd)/backup-set-report.sh"     "$set_id" --offhost-failed "remote checksum verification failed" || true
  exit 1
fi

# Only now does the set become visible under its final name: an interrupted transfer can never
# look like a completed remote backup.
if ! run_ssh "test ! -e '$remote_final' && mv '$remote_partial' '$remote_final'"; then
  echo "Off-host copy failed: the final remote set could not be published atomically." >&2
  exit 1
fi

cleanup_staging
trap - EXIT HUP INT TERM

script_dir="$(cd "$(dirname "$0")" && pwd)"
compose="${FINSPACE_COMPOSE:-docker compose}"
dump_filename="$(basename "$dump_path")"

# Evidence is written only after the set is genuinely published and hash-verified on the remote.
FINSPACE_BACKUP_ROOT="$backup_root" sh "$script_dir/backup-set-report.sh"   "$set_id" --offhost-verified "$label"

# psql lives in the tools image, not on the host. The audit row reuses the existing
# backup.remote.copy action so Stage B can correlate it with backup.created by SHA-256.
if ! $compose --profile tools run --rm backup   sh /scripts/backup-remote-audit.sh "$dump_filename" "$dump_sha" "$label" >/dev/null; then
  echo "Off-host copy warning: the set is published but backup.remote.copy was not recorded." >&2
  exit 1
fi

echo "Backup set $set_id published off-host (label=$label)."
