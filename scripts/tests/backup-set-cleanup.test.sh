#!/bin/sh
# Local set retention follows the canonical PostgreSQL policy instead of inventing a second one:
# a set lives exactly as long as the dump its inventory references.
#
# Everything unexpected is left for an operator. An automated deleter that guesses about malformed
# state is more dangerous than one that stops and says so.
set -eu

fail() {
  printf 'backup-set-cleanup test: FAIL: %s\n' "$1" >&2
  exit 1
}

assert_contains() {
  case "$1" in
    *"$2"*) ;;
    *) fail "$3" ;;
  esac
}

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

backup_root="$test_root/backups"
mkdir -p "$backup_root/database" "$backup_root/sets"

make_set() {
  set_id=$1
  dump_name=$2
  create_dump=$3
  mkdir -p "$backup_root/sets/$set_id"
  printf '{\n  "set_id": "%s",\n  "database": {\n      "path": "database/%s"\n    }\n}\n' \
    "$set_id" "$dump_name" >"$backup_root/sets/$set_id/backup-set.json"
  printf '{"local_verified": true}\n' >"$backup_root/sets/$set_id/backup-set-report.json"
  [ "$create_dump" != "yes" ] || printf 'dump' >"$backup_root/database/$dump_name"
}

run_cleanup() {
  FINSPACE_BACKUP_ROOT="$backup_root" sh "$repository_root/scripts/backup-set-cleanup.sh"
}

live_id="2026-09-05T010000Z"
expired_id="2026-09-01T010000Z"
malformed_id="2026-09-02T010000Z"
traversing_id="2026-09-03T010000Z"
unsafe_id="not-a-set"
partial_id=".2026-09-04T010000Z.partial"

make_set "$live_id" "finspace_${live_id}.dump" yes
make_set "$expired_id" "finspace_${expired_id}.dump" no

# C: a malformed inventory must never be interpreted.
mkdir -p "$backup_root/sets/$malformed_id"
printf 'not json at all\n' >"$backup_root/sets/$malformed_id/backup-set.json"

# D: a traversing inventory path must be refused rather than followed.
mkdir -p "$backup_root/sets/$traversing_id"
printf '{"database": {"path": "database/../../etc/finspace_x.dump"}}\n' \
  >"$backup_root/sets/$traversing_id/backup-set.json"

# E/F: an unrecognised directory and an in-flight partial are not sets.
mkdir -p "$backup_root/sets/$unsafe_id" "$backup_root/sets/$partial_id"
printf 'operator scratch\n' >"$backup_root/sets/$unsafe_id/notes.txt"
printf 'half written\n' >"$backup_root/sets/$partial_id/database.dump"

# The expired set carries an optional n8n archive that must go with it, and only with it.
printf 'n8n archive bytes' >"$backup_root/sets/$expired_id/n8n-data.tar.gz"
printf 'live n8n archive' >"$backup_root/sets/$live_id/n8n-data.tar.gz"

output=$(run_cleanup 2>&1) || fail "cleanup exited non-zero"

# A / G: the set whose canonical dump survives is retained, newest included.
[ -d "$backup_root/sets/$live_id" ] || fail "a set with a live canonical dump was removed"
[ -s "$backup_root/sets/$live_id/n8n-data.tar.gz" ] || fail "a live n8n archive was removed"

# B / F: the set whose canonical dump the database policy already deleted goes, archive included.
[ ! -d "$backup_root/sets/$expired_id" ] || fail "a set whose canonical dump expired was retained"

# C, D, E: anything unexpected is reported and left exactly where it is.
[ -s "$backup_root/sets/$malformed_id/backup-set.json" ] || fail "a malformed set was deleted"
[ -s "$backup_root/sets/$traversing_id/backup-set.json" ] || fail "a traversing inventory was acted on"
[ -s "$backup_root/sets/$unsafe_id/notes.txt" ] || fail "an unrecognised directory was deleted"
[ -s "$backup_root/sets/$partial_id/database.dump" ] || fail "an in-flight partial was treated as a set"
[ -e "/etc/finspace_x.dump" ] && fail "the traversing path escaped the backup root"

assert_contains "$output" "set_id=$malformed_id reason=malformed_inventory" "malformed set not reported"
assert_contains "$output" "set_id=$traversing_id reason=unsafe_inventory_path" "traversal not reported"
assert_contains "$output" "set_id=$unsafe_id reason=unrecognised_directory" "unknown directory not reported"
assert_contains "$output" "set_id=$expired_id reason=canonical_dump_expired" "removal not reported"
# The dot-prefixed partial is not even a candidate — the glob never sees it — which is why the
# skipped count is three rather than four.
assert_contains "$output" "backup_set_cleanup_finished removed=1 retained=1 skipped=3" "summary counts"

# A set directory missing its inventory is incomplete, not expired.
incomplete_id="2026-09-06T010000Z"
mkdir -p "$backup_root/sets/$incomplete_id"
output=$(run_cleanup 2>&1) || fail "cleanup exited non-zero on a second pass"
[ -d "$backup_root/sets/$incomplete_id" ] || fail "a set without an inventory was deleted"
assert_contains "$output" "set_id=$incomplete_id reason=missing_inventory" "incomplete set not reported"

# Idempotent: a second run with nothing newly expired removes nothing.
assert_contains "$output" "removed=0" "a repeat run removed something"

# An empty sets directory is not an error.
rm -rf "$backup_root/sets"
run_cleanup >/dev/null 2>&1 || fail "cleanup failed when no sets exist"

printf 'backup-set-cleanup test: PASS\n'
