#!/bin/sh
# Install or update the production Compose wrapper on the host.
#
# The same command performs the first install and every later update: it copies the wrapper from
# this checkout, so the host command can never be a hand-edited variant of it. Nothing is
# generated, nothing is templated, and no configuration is written outside the target file.
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
