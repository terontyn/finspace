#!/bin/sh
# Scheduled backup run: locking, deterministic dump handoff, and above all ordering.
#
# The orchestration is what Stage B adds, so the tests assert the exact sequence rather than mocking
# it away: retention must never run after a failed off-host copy, and the run must know precisely
# which dump it created instead of guessing at the newest file.
set -eu

fail() {
  printf 'backup-run test: FAIL: %s\n' "$1" >&2
  exit 1
}

assert_equal() {
  [ "$2" = "$1" ] || fail "$3: expected [$1], got [$2]"
}

assert_contains() {
  case "$1" in
    *"$2"*) ;;
    *) fail "$3" ;;
  esac
}

assert_absent() {
  case "$1" in
    *"$2"*) fail "$3" ;;
  esac
}

command -v flock >/dev/null 2>&1 || {
  printf 'backup-run test: SKIP (flock is unavailable in this environment)\n'
  exit 0
}

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

bin="$test_root/bin"
project="$test_root/project"
backup_root="$project/backups"
mkdir -p "$bin" "$project/scripts" "$backup_root/database" "$backup_root/sets"

# Release metadata comes from a fake git rather than a real fixture repository: what these tests
# need to prove is how the runner *invokes* git under a root-owned service, and a real repository
# would answer even without the scoped safe.directory the production defect required.
commit="4d0b1f9c2a7e5b3d8f6a0c4e2b9d7f1a3c5e8b02"

cp "$repository_root/scripts/backup-run.sh" "$project/scripts/backup-run.sh"
cp "$repository_root/scripts/backup-set-cleanup.sh" "$project/scripts/backup-set-cleanup.sh"

set_id="2026-09-03T010000Z"
dump_name="finspace_${set_id}.dump"

# The fake compose records every step in order and produces the artifacts the real tools would.
cat >"$bin/docker" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$STEP_LOG"
case "$*" in
  *"/scripts/backup.sh"*)
    [ ! -f "$CREATE_FAIL" ] || exit 1
    printf '%s\n' "$DUMP_NAME" >"$BACKUP_ROOT/database/.backup-run-result"
    printf 'dump payload' >"$BACKUP_ROOT/database/$DUMP_NAME"
    ;;
  *"/scripts/backup-set.sh"*)
    [ ! -f "$SET_FAIL" ] || exit 1
    mkdir -p "$BACKUP_ROOT/sets/$SET_ID"
    printf '{"path": "database/%s"}\n' "$DUMP_NAME" >"$BACKUP_ROOT/sets/$SET_ID/backup-set.json"
    ;;
  *"/scripts/backup-cleanup.sh"*)
    [ ! -f "$CLEANUP_FAIL" ] || exit 1
    ;;
esac
exit 0
STUB
# The off-host script is replaced by a recorder: this suite proves orchestration, and the transport
# itself has its own dedicated contract tests.
cat >"$bin/backup-offhost-stub.sh" <<'STUB'
#!/bin/sh
printf 'offhost %s\n' "$*" >>"$STEP_LOG"
[ ! -f "$OFFHOST_FAIL" ] || exit 1
exit 0
STUB
chmod 755 "$bin/docker" "$bin/backup-offhost-stub.sh"
cp "$bin/backup-offhost-stub.sh" "$project/scripts/backup-offhost.sh"

# A fake git that stands in for the production ownership refusal: it answers only when the caller
# has scoped safe.directory to the exact project root it is also operating on. Anything else — no
# override, a wildcard, the wrong directory — behaves like the root-owned systemd run did.
cat >"$bin/git" <<'STUB'
#!/bin/sh
printf 'git %s\n' "$*" >>"$GIT_LOG"
scoped=""
directory=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -c)
      [ "$2" != "safe.directory=$EXPECTED_ROOT" ] || scoped="yes"
      case "$2" in
        safe.directory=\*) printf 'wildcard safe.directory is forbidden\n' >&2; exit 128 ;;
      esac
      shift 2
      ;;
    -C) directory="$2"; shift 2 ;;
    *) break ;;
  esac
done
case "${1:-}" in
  config)
    printf 'the runner must never write git configuration\n' >&2
    exit 128
    ;;
esac
if [ "$scoped" != "yes" ] || [ "$directory" != "$EXPECTED_ROOT" ]; then
  printf 'fatal: detected dubious ownership in repository at %s\n' "$directory" >&2
  exit 128
fi
case "${1:-}" in
  rev-parse)
    [ ! -f "$GIT_REVPARSE_FAIL" ] || exit 128
    printf '%s\n' "$FAKE_COMMIT"
    ;;
  describe)
    [ ! -f "$GIT_DESCRIBE_FAIL" ] || exit 128
    printf '%s\n' "${FAKE_TAG:-}"
    ;;
  *) exit 0 ;;
esac
STUB
chmod 755 "$bin/git"

run_backup() {
  PATH="$bin:/usr/bin:/bin" \
  STEP_LOG="$test_root/steps.log" \
  DUMP_NAME="${DUMP_OVERRIDE-$dump_name}" \
  SET_ID="$set_id" \
  BACKUP_ROOT="$backup_root" \
  CREATE_FAIL="$test_root/create.fail" \
  SET_FAIL="$test_root/set.fail" \
  OFFHOST_FAIL="$test_root/offhost.fail" \
  CLEANUP_FAIL="$test_root/cleanup.fail" \
  GIT_LOG="$test_root/git.log" \
  EXPECTED_ROOT="$project" \
  FAKE_COMMIT="${COMMIT_OVERRIDE-$commit}" \
  FAKE_TAG="${TAG_OVERRIDE-}" \
  GIT_REVPARSE_FAIL="$test_root/revparse.fail" \
  GIT_DESCRIBE_FAIL="$test_root/describe.fail" \
  FINSPACE_PROJECT_ROOT="$project" \
  FINSPACE_BACKUP_ROOT="$backup_root" \
  FINSPACE_BACKUP_LOCK_FILE="$test_root/backup.lock" \
  FINSPACE_BACKUP_OFFHOST_ENABLED="${OFFHOST_ENABLED-true}" \
  sh "$project/scripts/backup-run.sh" "$@"
}

reset_run() {
  : >"$test_root/steps.log"
  : >"$test_root/git.log"
  rm -rf "$backup_root/sets"/* "$backup_root/database"/*
  mkdir -p "$backup_root/sets"
}

step_index() {
  grep -n "$1" "$test_root/steps.log" | head -n 1 | cut -d: -f1
}

# --- off-host enabled, everything succeeds ------------------------------------------------------
reset_run
output=$(run_backup 2>&1) || fail "a healthy run exited non-zero"

create_at=$(step_index "/scripts/backup.sh")
set_at=$(step_index "/scripts/backup-set.sh")
offhost_at=$(step_index "^offhost ")
db_cleanup_at=$(step_index "/scripts/backup-cleanup.sh")
[ -n "$create_at" ] || fail "the database backup was never created"
[ -n "$set_at" ] || fail "the backup set was never built"
[ -n "$offhost_at" ] || fail "the off-host copy never ran"
[ -n "$db_cleanup_at" ] || fail "database retention never ran"
[ "$create_at" -lt "$set_at" ] || fail "the set was built before the dump existed"
[ "$set_at" -lt "$offhost_at" ] || fail "the off-host copy ran before local verification"
[ "$offhost_at" -lt "$db_cleanup_at" ] || fail "retention ran before the off-host copy"

assert_contains "$output" "backup_run_started" "the start marker is missing"
assert_contains "$output" "backup_run_local_verified set_id=$set_id" "local verification marker"
assert_contains "$output" "backup_run_offhost_verified set_id=$set_id" "off-host marker"
assert_contains "$output" "backup_run_retention_finished" "retention marker"
assert_contains "$output" "backup_run_finished" "the finish marker is missing"
assert_contains "$output" "backup_set_cleanup_finished" "set retention never ran"
# The set survives because its canonical dump is still present.
[ -d "$backup_root/sets/$set_id" ] || fail "set retention removed a live set"

# The dump is identified by the deterministic handoff, not by scanning for the newest file.
assert_contains "$(cat "$test_root/steps.log")" "BACKUP_RESULT_FILE=/backups/database/.backup-run-result" \
  "the run did not request a deterministic dump handoff"
assert_contains "$(cat "$test_root/steps.log")" "/backups/database/$dump_name" \
  "the set was not built from the exact created dump"
# Release metadata is resolved from the checkout, so no operator has to export it.
assert_contains "$(cat "$test_root/steps.log")" "FINSPACE_COMMIT=$commit" "the commit was not resolved"

# --- off-host enabled but failing: no retention at all -------------------------------------------
reset_run
: >"$test_root/offhost.fail"
if output=$(run_backup 2>&1); then
  fail "a failed off-host copy exited zero"
fi
rm "$test_root/offhost.fail"

assert_contains "$output" "backup_run_failed reason=offhost_copy_failed" "the failure reason"
[ -n "$(step_index '/scripts/backup.sh')" ] || fail "the dump was not created"
[ -n "$(step_index '/scripts/backup-set.sh')" ] || fail "the set was not built"
[ -n "$(step_index '^offhost ')" ] || fail "the off-host copy was not attempted"
# The whole point: previous restore points must survive a bad new run.
[ -z "$(step_index '/scripts/backup-cleanup.sh')" ] || fail "database retention ran after a failed off-host copy"
assert_absent "$output" "backup_set_cleanup_finished" "set retention ran after a failed off-host copy"

# --- off-host explicitly disabled: degraded but successful ---------------------------------------
reset_run
OFFHOST_ENABLED=false output=$(OFFHOST_ENABLED=false run_backup 2>&1) ||
  fail "the degraded local-only run exited non-zero"

[ -z "$(step_index '^offhost ')" ] || fail "the off-host copy ran while disabled"
assert_contains "$output" "backup_run_offhost_skipped offhost_disabled=true" "the skip marker"
assert_contains "$output" "backup_run_degraded offhost_disabled=true" "the degraded warning"
[ -n "$(step_index '/scripts/backup-cleanup.sh')" ] || fail "retention did not run in local-only mode"
assert_contains "$output" "backup_set_cleanup_finished" "set retention did not run in local-only mode"
assert_contains "$output" "backup_run_finished" "the degraded run did not finish"

# --- refusals -------------------------------------------------------------------------------------
reset_run
if OFFHOST_ENABLED=maybe run_backup >/dev/null 2>&1; then
  fail "an invalid off-host flag was accepted"
fi

reset_run
: >"$test_root/create.fail"
if output=$(run_backup 2>&1); then
  fail "a failed dump creation exited zero"
fi
rm "$test_root/create.fail"
assert_contains "$output" "backup_run_failed reason=database_backup_failed" "creation failure reason"
[ -z "$(step_index '/scripts/backup-set.sh')" ] || fail "the set was built after a failed dump"

reset_run
: >"$test_root/set.fail"
if output=$(run_backup 2>&1); then
  fail "a failed set build exited zero"
fi
rm "$test_root/set.fail"
assert_contains "$output" "backup_run_failed reason=backup_set_failed" "set failure reason"
[ -z "$(step_index '^offhost ')" ] || fail "the off-host copy ran after a failed set build"

reset_run
if output=$(DUMP_OVERRIDE="../../etc/passwd" run_backup 2>&1); then
  fail "an unsafe dump handoff was accepted"
fi
assert_contains "$output" "backup_run_failed reason=dump_handoff_unsafe" "unsafe handoff reason"

# --- locking ---------------------------------------------------------------------------------------
reset_run
lock="$test_root/backup.lock"
: >"$lock"
# Hold the lock the way a running backup would, then prove a second run refuses to start.
exec 9>"$lock"
flock --nonblock 9 || fail "the test could not take the lock"
if output=$(run_backup 2>&1); then
  exec 9>&-
  fail "a second concurrent run started while the lock was held"
fi
exec 9>&-
assert_contains "$output" "backup_run_locked lock=busy" "the contention marker is missing"
[ -z "$(step_index '/scripts/backup.sh')" ] || fail "a locked-out run still created a dump"

# The lock is released once the holder is gone, so the next scheduled run proceeds normally.
reset_run
run_backup >/dev/null 2>&1 || fail "the run did not proceed after the lock was released"
[ -n "$(step_index '/scripts/backup.sh')" ] || fail "the follow-up run created no dump"

# And it is released after a failure too, not held until reboot.
reset_run
: >"$test_root/set.fail"
run_backup >/dev/null 2>&1 || true
rm "$test_root/set.fail"
reset_run
run_backup >/dev/null 2>&1 || fail "the lock was not released after a failed run"

# No secrets are ever echoed.
assert_absent "$(cat "$test_root/steps.log")" "PASSWORD" "a secret reached the log"
assert_absent "$(cat "$test_root/steps.log")" "JWT_SECRET" "a secret reached the log"


# --- Git metadata under a root-owned service ------------------------------------------------------
# Production runs the service as root against a checkout owned by the operator, so Git refuses the
# repository under its ownership protection. The fake git above reproduces that refusal exactly: it
# answers only when the caller scoped safe.directory to the same directory it is operating on.
reset_run
output=$(run_backup 2>&1) || fail "the runner could not read Git metadata"

git_log=$(cat "$test_root/git.log")
assert_contains "$git_log" "-c safe.directory=$project -C $project rev-parse HEAD" \
  "rev-parse did not carry a command-scoped safe.directory"
assert_contains "$git_log" "-c safe.directory=$project -C $project describe --exact-match --tags" \
  "describe did not carry a command-scoped safe.directory"
# The scope must be exactly the directory being read, and nothing broader.
assert_absent "$git_log" "safe.directory=*" "a wildcard safe.directory was used"
assert_absent "$git_log" "config" "the runner touched Git configuration"
assert_absent "$git_log" "--global" "the runner used a global Git setting"
assert_contains "$(cat "$test_root/steps.log")" "FINSPACE_COMMIT=$commit" \
  "the commit was not obtained through the scoped command"

# An exact tag is passed through; anything unsafe becomes empty rather than reaching the inventory.
reset_run
TAG_OVERRIDE="local-v0.16" run_backup >/dev/null 2>&1 || fail "a tagged checkout failed"
assert_contains "$(cat "$test_root/steps.log")" "FINSPACE_TAG=local-v0.16" "the exact tag was lost"

reset_run
TAG_OVERRIDE="v1.0; rm -rf /" run_backup >/dev/null 2>&1 || fail "an unsafe tag broke the run"
assert_absent "$(cat "$test_root/steps.log")" "rm -rf" "an unsafe tag reached the set build"

reset_run
: >"$test_root/describe.fail"
run_backup >/dev/null 2>&1 || fail "an untagged checkout must still back up"
rm "$test_root/describe.fail"
assert_contains "$(cat "$test_root/steps.log")" "FINSPACE_TAG=" "an untagged run recorded no tag field"

# Inherited environment must never stand in for the checkout: a scheduled run has no operator, and
# a stale exported value would describe a different release entirely.
reset_run
FINSPACE_COMMIT=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef FINSPACE_TAG=local-v9.9 \
  run_backup >/dev/null 2>&1 || fail "the run failed with inherited metadata present"
steps=$(cat "$test_root/steps.log")
assert_contains "$steps" "FINSPACE_COMMIT=$commit" "an inherited commit replaced the checkout"
assert_absent "$steps" "deadbeefdeadbeef" "an inherited commit replaced the checkout"
assert_absent "$steps" "local-v9.9" "an inherited tag replaced the checkout"

# --- Git metadata failure still fails closed -------------------------------------------------------
reset_run
: >"$test_root/revparse.fail"
if output=$(run_backup 2>&1); then
  rm "$test_root/revparse.fail"
  fail "an unreadable checkout still ran a backup"
fi
rm "$test_root/revparse.fail"
assert_contains "$output" "backup_run_failed reason=commit_unavailable" "the failure reason changed"
[ -z "$(step_index '/scripts/backup.sh')" ] || fail "a dump was created without release metadata"

# A project root that does not exist is refused before Git is ever consulted.
reset_run
if output=$(FINSPACE_PROJECT_ROOT="$test_root/not-a-checkout" \
  PATH="$bin:/usr/bin:/bin" \
  STEP_LOG="$test_root/steps.log" GIT_LOG="$test_root/git.log" EXPECTED_ROOT="$project" \
  FAKE_COMMIT="$commit" BACKUP_ROOT="$backup_root" \
  FINSPACE_BACKUP_ROOT="$backup_root" \
  FINSPACE_BACKUP_LOCK_FILE="$test_root/backup.lock" \
  FINSPACE_BACKUP_OFFHOST_ENABLED=false \
  sh "$project/scripts/backup-run.sh" 2>&1); then
  fail "a missing project root was accepted"
fi
assert_contains "$output" "backup_run_failed reason=project_root_missing" "missing root reason"
assert_equal "" "$(cat "$test_root/git.log")" "Git was consulted for a missing project root"

printf 'backup-run test: PASS\n'
