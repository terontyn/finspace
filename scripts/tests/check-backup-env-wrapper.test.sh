#!/bin/sh
# The backup.env wrapper check exists because of a real production failure: a host upgraded past
# the legacy wrapper kept an absolute FINSPACE_COMPOSE pointing at the retired path, and the
# scheduled backup failed silently for days. The properties worth testing are therefore what it
# detects, what it deliberately tolerates, and — most of all — that it never edits the file.
set -eu

fail() {
  printf 'check-backup-env-wrapper test: FAIL: %s\n' "$1" >&2
  exit 1
}

assert_contains() {
  case "$1" in
    *"$2"*) ;;
    *) fail "$3" ;;
  esac
}

assert_missing() {
  case "$1" in
    *"$2"*) fail "$3" ;;
  esac
}

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
checker="$repository_root/scripts/check-backup-env-wrapper.sh"
[ -f "$checker" ] || fail "the checker is missing: $checker"

test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

mkdir -p "$test_root/usr-local-bin" "$test_root/usr-local-sbin"
wrapper="$test_root/usr-local-bin/finspace-compose"
printf '#!/bin/sh\nexit 0\n' >"$wrapper"
chmod +x "$wrapper"

run() {
  status=0
  output=$(sh "$checker" --wrapper "$wrapper" --backup-env "$1" 2>&1) || status=$?
}

# --- the retired absolute path is the whole point --------------------------------------------------
stale="$test_root/stale.env"
cat >"$stale" <<'ENV'
FINSPACE_BACKUP_ROOT=/opt/finspace/backups
FINSPACE_COMPOSE=/usr/local/sbin/finspace-compose
FINSPACE_BACKUP_OFFHOST_ENABLED=false
ENV
before=$(cat "$stale")
run "$stale"
[ "$status" -eq 3 ] || fail "a stale absolute wrapper path was not reported (exit $status)"
assert_contains "$output" "REPAIR REQUIRED" "the diagnostic did not name the repair"
assert_contains "$output" "$wrapper" "the diagnostic did not name the canonical wrapper"
assert_contains "$output" "finspace-backup.service" "the diagnostic did not require a backup run"
[ "$before" = "$(cat "$stale")" ] || fail "the checker modified backup.env"

# The exit code has to be distinguishable from a usage error, or a deploy cannot react to it.
[ "$status" -ne 2 ] || fail "repair required is indistinguishable from a usage error"

# --- an absolute path that exists but is not the canonical one -------------------------------------
other="$test_root/usr-local-sbin/finspace-compose"
printf '#!/bin/sh\nexit 0\n' >"$other"
chmod +x "$other"
printf 'FINSPACE_COMPOSE=%s\n' "$other" >"$test_root/other.env"
run "$test_root/other.env"
[ "$status" -eq 3 ] || fail "a second executable wrapper was accepted as canonical"
assert_contains "$output" "canonical wrapper" "the ambiguity was not explained"

# --- the documented configurations are accepted ----------------------------------------------------
printf 'FINSPACE_COMPOSE=finspace-compose\n' >"$test_root/bare.env"
run "$test_root/bare.env"
[ "$status" -eq 0 ] || fail "the documented bare command name was rejected: $output"
assert_contains "$output" "PATH" "the reason for accepting a bare name was not stated"

printf 'FINSPACE_COMPOSE="%s"\n' "$wrapper" >"$test_root/quoted.env"
run "$test_root/quoted.env"
[ "$status" -eq 0 ] || fail "a quoted canonical path was rejected: $output"

printf "FINSPACE_COMPOSE='%s'\n" "$wrapper" >"$test_root/single.env"
run "$test_root/single.env"
[ "$status" -eq 0 ] || fail "a single-quoted canonical path was rejected: $output"

printf 'FINSPACE_BACKUP_ROOT=/opt/finspace/backups\n' >"$test_root/unset.env"
run "$test_root/unset.env"
[ "$status" -eq 0 ] || fail "an unset FINSPACE_COMPOSE was rejected: $output"

# A host that never configured scheduled backups has nothing to repair.
run "$test_root/absent.env"
[ "$status" -eq 0 ] || fail "an absent backup.env was treated as a fault: $output"

# --- the last assignment wins, exactly as systemd reads the file -----------------------------------
cat >"$test_root/repaired.env" <<ENV
FINSPACE_COMPOSE=/usr/local/sbin/finspace-compose
FINSPACE_COMPOSE=$wrapper
ENV
run "$test_root/repaired.env"
[ "$status" -eq 0 ] || fail "a repaired file was still reported as stale: $output"

cat >"$test_root/regressed.env" <<ENV
FINSPACE_COMPOSE=$wrapper
FINSPACE_COMPOSE=/usr/local/sbin/finspace-compose
ENV
run "$test_root/regressed.env"
[ "$status" -eq 3 ] || fail "only the first assignment was read"

# --- a missing canonical wrapper is itself a repair --------------------------------------------------
status=0
output=$(sh "$checker" --wrapper "$test_root/usr-local-bin/absent" \
  --backup-env "$test_root/bare.env" 2>&1) || status=$?
[ "$status" -eq 3 ] || fail "a missing canonical wrapper was accepted"
assert_contains "$output" "install-finspace-compose.sh" "the install command was not named"

# --- argument handling -----------------------------------------------------------------------------
status=0
output=$(sh "$checker" --wrapper relative/path 2>&1) || status=$?
[ "$status" -eq 2 ] || fail "a relative wrapper path was accepted"
status=0
output=$(sh "$checker" --unknown 2>&1) || status=$?
[ "$status" -eq 2 ] || fail "an unknown argument was accepted"
status=0
output=$(sh "$checker" --wrapper 2>&1) || status=$?
[ "$status" -eq 2 ] || fail "a flag without its value was accepted"

# --- it is a diagnostic, not a repair tool -----------------------------------------------------------
source_text=$(cat "$checker")
for forbidden in "sed -i" "tee " "mv " "rm " "systemctl "; do
  assert_missing "$source_text" "$forbidden" "the checker contains a host-mutating command: $forbidden"
done
assert_missing "$source_text" ">\"\$backup_env\"" "the checker writes to backup.env"

printf 'check-backup-env-wrapper test: PASS\n'
