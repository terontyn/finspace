#!/bin/sh
# Install or update the production Compose wrapper on the host.
#
# The same command performs the first install and every later update: it copies the wrapper from
# this checkout, so the host command can never be a hand-edited variant of it. Nothing is
# generated, nothing is templated, and no configuration is written outside the target file.
#
# It also reports one host-configuration hazard it can see but must never fix by itself. Hosts
# installed before the wrapper moved to /usr/local/bin can still carry an absolute
# FINSPACE_COMPOSE=... in /etc/finspace/backup.env pointing at the retired location. Installing the
# new wrapper does not repair that line, and the next scheduled backup then fails silently until
# somebody reads the journal. So check-backup-env-wrapper.sh inspects that file and never writes it:
# rewriting host configuration from a repository script would be a far worse habit than asking the
# operator to change one line. A host that needs that repair exits 3, so a deploy running under
# `set -e` stops instead of declaring the upgrade complete.
set -eu

umask 022

fail() {
  printf 'finspace-compose install: FAIL: %s\n' "$1" >&2
  exit 1
}

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
source_file="$script_directory/finspace-compose.sh"
target_file="${FINSPACE_COMPOSE_BIN:-/usr/local/bin/finspace-compose}"

if [ "$#" -gt 0 ]; then
  fail "usage: $0 (set FINSPACE_COMPOSE_BIN to install elsewhere)"
fi
[ "$(id -u)" -eq 0 ] || fail "run this command as root"
[ -f "$source_file" ] || fail "wrapper source is missing: $source_file"
case "$target_file" in
  /*) ;;
  *) fail "FINSPACE_COMPOSE_BIN must be an absolute path" ;;
esac
target_directory=${target_file%/*}
[ -d "$target_directory" ] || fail "target directory does not exist: $target_directory"

install -o root -g root -m 0755 "$source_file" "$target_file"

# The host command must be this checkout's wrapper, byte for byte. A mismatch here means the
# install did not take effect and must not be reported as success.
cmp -s "$source_file" "$target_file" || fail "installed wrapper does not match the repository copy"

printf 'finspace-compose install: PASS: %s\n' "$target_file"

# The wrapper is in place; whether the host's scheduled backup can still reach it is a separate
# question, and it has its own read-only diagnostic. FINSPACE_BACKUP_ENV exists so the shell test
# can exercise this path without touching /etc.
checker="$script_directory/check-backup-env-wrapper.sh"
[ -f "$checker" ] || fail "backup.env checker is missing: $checker"
check_status=0
sh "$checker" --wrapper "$target_file" \
  --backup-env "${FINSPACE_BACKUP_ENV:-/etc/finspace/backup.env}" >/dev/null || check_status=$?
[ "$check_status" -eq 0 ] || exit "$check_status"
