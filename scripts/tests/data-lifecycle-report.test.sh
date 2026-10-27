#!/bin/sh
# The host-side lifecycle report is pure diagnosis, so the properties worth testing are what it
# refuses to do and what it refuses to pretend: it must never delete, never follow a link out of a
# managed path, never print a filename, and never report an unreadable directory as empty.
set -eu

fail() {
  printf 'data-lifecycle-report test: FAIL: %s\n' "$1" >&2
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
report="$repository_root/scripts/data-lifecycle-report.sh"

test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

bin="$test_root/bin"
project="$test_root/opt/finspace project"
mkdir -p "$bin" \
  "$project/backups/database" "$project/backups/sets/2026-09-01T010000Z" \
  "$project/backups/acceptance-reports" "$project/data/imports" "$project/data/acceptance"

# A fake compose records what the report asked it to run and never starts anything.
cat >"$bin/finspace-compose" <<'STUB'
#!/bin/sh
printf 'compose %s\n' "$*" >>"$COMPOSE_LOG"
[ "${STUB_COMPOSE_FAILS:-false}" = "false" ] || exit 1
printf 'stub container output\n'
exit 0
STUB
chmod 755 "$bin/finspace-compose"

# Fixture content, with sizes that are easy to check.
dd if=/dev/zero of="$project/backups/database/finspace_2026-09-01T010000Z.dump" bs=1 count=1000 \
  >/dev/null 2>&1
printf '%s' "0123456789" >"$project/backups/database/finspace_2026-09-01T010000Z.dump.manifest.json"
cat >"$project/backups/sets/2026-09-01T010000Z/backup-set.json" <<'JSON'
{ "set_id": "2026-09-01T010000Z", "alembic_revision": "0017_categorization_history" }
JSON
cat >"$project/backups/sets/2026-09-01T010000Z/backup-set-report.json" <<'JSON'
{ "set_id": "2026-09-01T010000Z", "local_verified": true, "offhost_verified": false }
JSON
printf 'personal-statement-content' \
  >"$project/data/imports/0123456789abcdef0123456789abcdef.csv"
printf 'evidence' >"$project/data/acceptance/dr-restore-2026-09-01T000000Z.json"

run_report() {
  : >"$test_root/compose.log"
  status=0
  output=$(
    PATH="$bin:/usr/bin:/bin" \
    COMPOSE_LOG="$test_root/compose.log" \
    STUB_COMPOSE_FAILS="${STUB_COMPOSE_FAILS:-false}" \
    sh "$report" --project-root "$project" "$@" 2>&1
  ) || status=$?
}

# --- it is executable and destroys nothing ----------------------------------------------------
[ -x "$report" ] || fail "scripts/data-lifecycle-report.sh is not executable in the checkout"
body=$(grep -v '^[[:space:]]*#' "$report")
# Looked for where a command can actually start, not as loose substrings: `--rm` in
# `compose run --rm` is a throwaway container and is exactly what this should be doing.
mutators=$(
  printf '%s\n' "$body" |
    grep -nE '(^|[;&|]|\bthen[[:space:]]|\bdo[[:space:]])[[:space:]]*(rm|rmdir|unlink|truncate|shred|chmod|chown|mv|cp|dd|tee)[[:space:]]' ||
    true
)
[ -z "$mutators" ] || fail "the report runs a mutating command: $mutators"
for forbidden in "prune" "down -v" "volume rm" "--apply" "--overwrite-main"; do
  assert_missing "$body" "$forbidden" "the report contains a destructive operation: $forbidden"
done
# find is used to measure, never to act.
assert_missing "$body" "-delete" "the report deletes through find"
assert_missing "$body" "-exec" "the report executes through find"

# --- a normal run -------------------------------------------------------------------------------
run_report
[ "$status" -eq 0 ] || fail "a normal run exited $status: $output"
assert_contains "$output" "MANAGED DIRECTORIES" "no managed directory section"
assert_contains "$output" "backups/database" "backups/database was not measured"
assert_contains "$output" "data/imports" "data/imports was not measured"
assert_contains "$output" "2 files         1010 bytes" "the dump and its manifest were not totalled"
assert_contains "$output" "backup-cleanup.sh: 7 daily + 4 weekly" "the retention owner was not named"
assert_contains "$output" "complete" "a complete run did not say so"

# Sizes and counts, never names. A staged upload name is opaque, but printing it is still not this
# tool's job, and an acceptance artifact name can carry a date and a drill id.
assert_missing "$output" "0123456789abcdef0123456789abcdef.csv" "a staged filename was printed"
assert_missing "$output" "dr-restore-2026-09-01T000000Z.json" "an evidence filename was printed"
assert_missing "$output" "personal-statement-content" "file contents were read"

# --- recorded backup facts are reported, not judged ---------------------------------------------
assert_contains "$output" "0017_categorization_history" "the recorded revision was not shown"
assert_contains "$output" "local_verified               true" "local_verified was not shown"
assert_contains "$output" "offhost_verified             false" "offhost_verified was not shown"
assert_contains "$output" "not a verification" "the report implies it verified the backup"
assert_missing "$output" "backup is valid" "the report declares a backup valid"

# --- it reuses the existing tools rather than reimplementing them -------------------------------
compose_log=$(cat "$test_root/compose.log")
assert_contains "$compose_log" "scripts/data_lifecycle_report.py" "the database report was not invoked"
assert_contains "$compose_log" "scripts/import_staging_reclaim.py" "F010 inspection was not invoked"
assert_contains "$compose_log" "run --rm --no-deps backend" "a throwaway container was not used"
# F010 must be asked to inspect, never to delete.
assert_missing "$compose_log" "--apply" "the report asked F010 to delete"

# --- an unreadable directory is never reported as empty -----------------------------------------
# Probed rather than assumed: root reads everything, and a Windows checkout ignores chmod, so on
# those platforms this case cannot be staged at all.
chmod 000 "$project/backups/database" 2>/dev/null || true
if [ -r "$project/backups/database" ] && [ -x "$project/backups/database" ]; then
  chmod 755 "$project/backups/database" 2>/dev/null || true
  printf 'data-lifecycle-report test: note: permissions are not enforced here; that case was skipped\n'
else
  run_report
  chmod 755 "$project/backups/database"
  [ "$status" -ne 0 ] || fail "an unreadable directory did not make the run partial"
  assert_contains "$output" "UNREADABLE" "an unreadable directory was not called out"
  assert_contains "$output" "PARTIAL" "a partial run reported itself complete"
  assert_missing "$output" "0 files            0 bytes  backup-cleanup" \
    "an unreadable directory was reported as empty"
fi

# --- a symlinked managed path is refused ---------------------------------------------------------
outside="$test_root/outside"
mkdir -p "$outside"
printf 'huge' >"$outside/secret.csv"
rm -rf -- "$project/data/acceptance"
ln -s "$outside" "$project/data/acceptance" 2>/dev/null || true
# Probed, not assumed: a Windows checkout silently copies instead of linking.
if [ -L "$project/data/acceptance" ]; then
  run_report
  [ "$status" -ne 0 ] || fail "a symlinked managed path did not make the run partial"
  assert_contains "$output" "SYMLINK" "the symlinked path was not refused"
  assert_missing "$output" "secret.csv" "the symlink was followed"
  [ -f "$outside/secret.csv" ] || fail "the link target was modified"
  rm -f -- "$project/data/acceptance"
else
  printf 'data-lifecycle-report test: note: symlinks are not supported here; that case was skipped\n'
  rm -rf -- "$project/data/acceptance"
fi
mkdir -p "$project/data/acceptance"

# --- a dangling root symlink is refused, not called absent ---------------------------------------
rm -rf -- "$project/data/acceptance"
ln -s "$test_root/never-created" "$project/data/acceptance" 2>/dev/null || true
if [ -L "$project/data/acceptance" ]; then
  [ ! -e "$project/data/acceptance" ] || fail "the dangling-link fixture is not dangling"
  run_report
  [ "$status" -ne 0 ] || fail "a dangling root symlink did not make the run partial"
  assert_contains "$output" "SYMLINK" "a dangling symlink was not refused"
  assert_missing "$output" "data/acceptance             absent" \
    "a dangling symlink was reported as an absent path"
  rm -f -- "$project/data/acceptance"
else
  printf 'data-lifecycle-report test: note: symlinks are not supported here; dangling case skipped\n'
  rm -rf -- "$project/data/acceptance"
fi
mkdir -p "$project/data/acceptance"

# --- an unreadable subdirectory makes the total a lower bound --------------------------------------
# find keeps going past a subdirectory it cannot enter, so its total would otherwise be published
# as if the whole tree had been measured.
mkdir -p "$project/backups/sets/2026-09-02T010000Z"
printf 'x' >"$project/backups/sets/2026-09-02T010000Z/backup-set.json"
chmod 000 "$project/backups/sets/2026-09-02T010000Z" 2>/dev/null || true
if [ -r "$project/backups/sets/2026-09-02T010000Z" ] && [ -x "$project/backups/sets/2026-09-02T010000Z" ]; then
  chmod 755 "$project/backups/sets/2026-09-02T010000Z" 2>/dev/null || true
  printf 'data-lifecycle-report test: note: permissions are not enforced here; subtree case skipped\n'
else
  run_report
  chmod 755 "$project/backups/sets/2026-09-02T010000Z"
  [ "$status" -ne 0 ] || fail "an unreadable subtree did not make the run partial"
  assert_contains "$output" "PARTIAL: part of the tree could not be read" \
    "an unreadable subtree was not called out"
  assert_contains "$output" "PARTIAL" "the run did not report itself partial"
fi
rm -rf -- "$project/backups/sets/2026-09-02T010000Z"

# --- content below the traversal depth is a lower bound, not a total -------------------------------
mkdir -p "$project/data/imports/nested"
printf 'never counted' >"$project/data/imports/nested/deep.csv"
run_report
[ "$status" -ne 0 ] || fail "untraversed nested content did not make the run partial"
assert_contains "$output" "PARTIAL: content below depth 1 was not traversed" \
  "nested content beyond the traversal depth was not called out"
assert_missing "$output" "deep.csv" "a nested filename was printed"
rm -rf -- "$project/data/imports/nested"

# --- a complete directory is still reported as complete --------------------------------------------
run_report
[ "$status" -eq 0 ] || fail "a fully measurable tree was reported partial: $output"
assert_missing "$output" "PARTIAL" "a complete run reported a partial directory"
assert_contains "$output" "2 files         1010 bytes" "the complete total lost its exact value"

# --- an absent path is stated, not invented -------------------------------------------------------
rm -rf -- "$project/backups/acceptance-reports"
run_report
assert_contains "$output" "absent" "a missing directory was not reported as absent"
mkdir -p "$project/backups/acceptance-reports"

# --- the container tools failing fails the report ---------------------------------------------------
STUB_COMPOSE_FAILS=true run_report
[ "$status" -ne 0 ] || fail "a failing container tool did not fail the report"
assert_contains "$output" "PARTIAL" "a failed section still claimed completeness"

# --- the filesystem view works with no containers at all ---------------------------------------------
run_report --no-database --no-imports
[ "$status" -eq 0 ] || fail "the filesystem-only run failed: $output"
assert_contains "$output" "skipped by --no-database" "the skip was not stated"
[ ! -s "$test_root/compose.log" ] || fail "a container was started despite --no-database"

# --- argument handling ---------------------------------------------------------------------------
status=0
output=$(PATH="$bin:/usr/bin:/bin" sh "$report" --project-root relative/path 2>&1) || status=$?
[ "$status" -eq 2 ] || fail "a relative project root was accepted"
status=0
output=$(PATH="$bin:/usr/bin:/bin" sh "$report" --project-root "$test_root/absent" 2>&1) || status=$?
[ "$status" -eq 1 ] || fail "a missing project root was accepted"

printf 'data-lifecycle-report test: PASS\n'
