#!/bin/sh
# The disaster-recovery drill is the one script whose failure mode is losing production, so what
# it refuses matters more than what it does. Everything here runs against fake docker and
# finspace-compose commands in a temporary directory: no real container, volume, database or
# production path is touched, and the test removes only what it created.
set -eu

fail() {
  printf 'dr-restore-drill test: FAIL: %s\n' "$1" >&2
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

assert_missing() {
  case "$1" in
    *"$2"*) fail "$3" ;;
  esac
}

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
drill="$repository_root/scripts/dr-restore-drill.sh"
probe_script="$repository_root/scripts/dr-data-probe.sh"

test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

bin="$test_root/bin"
mkdir -p "$bin"

# A space in the path is not decorative: it is where naive quoting breaks.
project="$test_root/opt/finspace drill"
sets_root="$test_root/transfer"
mkdir -p "$project/backend/alembic/versions" "$project/backend/scripts" \
  "$project/data/acceptance" "$project/backups/database" "$sets_root"
: >"$project/docker-compose.yml"
: >"$project/compose.production.yml"
: >"$project/backend/scripts/validate_compose_topology.py"

# A production .env sitting in the checkout, with a value that must never surface anywhere.
planted_secret="PLANTED-SECRET-b4d1f00d"
printf 'JWT_SECRET_KEY=%s\nPOSTGRES_PASSWORD=%s\n' "$planted_secret" "$planted_secret" \
  >"$project/.env"
# Sentinels: the drill must not delete anything it did not create.
: >"$project/backups/database/.keep-me"
: >"$project/.operator-file"

# --- the target release's migration graph -------------------------------------------------------
write_revision() {
  file="$project/backend/alembic/versions/$1.py"
  if [ "$2" = "none" ]; then
    parent="None"
  else
    parent="\"$2\""
  fi
  cat >"$file" <<EOF
"""fixture revision"""

revision: str = "$1"
down_revision: str | None = $parent
EOF
}
write_revision 0015_alpha none
write_revision 0016_beta 0015_alpha
write_revision 0017_gamma 0016_beta
target_head="0017_gamma"

# --- fake docker --------------------------------------------------------------------------------
: >"$test_root/containers"
: >"$test_root/volumes"
cat >"$bin/docker" <<'STUB'
#!/bin/sh
printf 'docker %s\n' "$*" >>"$DOCKER_LOG"
case "$*" in
  *"volume ls"*) cat "$STUB_VOLUMES" ;;
  *"ps -a"*) cat "$STUB_CONTAINERS" ;;
esac
exit 0
STUB

# --- fake finspace-compose ----------------------------------------------------------------------
cat >"$bin/finspace-compose" <<'STUB'
#!/bin/sh
printf 'compose %s\n' "$*" >>"$COMPOSE_LOG"
case "$*" in
  *"dr-data-probe.sh --schema-state"*)
    cat "$STUB_DB_STATE"
    ;;
  *"dr-data-probe.sh"*)
    [ "${STUB_PROBE_FAILS:-false}" = "false" ] || exit 1
    cat "$STUB_RESTORED_PROBE"
    ;;
  *restore.sh*)
    [ "${STUB_RESTORE_FAILS:-false}" = "false" ] || { echo "restore blew up" >&2; exit 1; }
    printf 'tables=%s\nrevision=%s\n' "42" "$STUB_BACKUP_REVISION" >"$STUB_DB_STATE"
    ;;
  *"alembic upgrade head"*)
    printf 'tables=%s\nrevision=%s\n' "42" "$STUB_TARGET_HEAD" >"$STUB_DB_STATE"
    ;;
  *"alembic current"*)
    revision=$(sed -n 's/^revision=\(.*\)$/\1/p' "$STUB_DB_STATE")
    printf '%s (head)\n' "$revision"
    ;;
  *"config --format json"*)
    printf '{}\n'
    ;;
  *"ps --status running --services"*)
    cat "$STUB_SERVICES"
    ;;
esac
exit 0
STUB

# A fake git that reproduces the ownership refusal a root-run drill meets on an operator-owned
# checkout: it answers only when the caller scoped safe.directory to the very directory it is
# operating on, and exits 128 otherwise.
cat >"$bin/git" <<'STUB'
#!/bin/sh
printf 'git %s\n' "$*" >>"$GIT_LOG"
[ "$1" = "-c" ] || exit 128
[ "$2" = "safe.directory=$STUB_PROJECT" ] || exit 128
shift 2
[ "$1" = "-C" ] || exit 128
[ "$2" = "$STUB_PROJECT" ] || exit 128
shift 2
case "$1" in
  rev-parse) printf '%s\n' "$STUB_TARGET_COMMIT" ;;
  describe) [ -n "${STUB_TARGET_TAG:-}" ] || exit 1; printf '%s\n' "$STUB_TARGET_TAG" ;;
  *) exit 128 ;;
esac
exit 0
STUB

cat >"$bin/curl" <<'STUB'
#!/bin/sh
printf '%s' "${STUB_HTTP_STATUS:-200}"
exit 0
STUB

cat >"$bin/python3" <<'STUB'
#!/bin/sh
# Stands in for the topology validator only; the drill pipes compose config into it.
cat >/dev/null
exit "${STUB_TOPOLOGY_EXIT:-0}"
STUB

chmod 755 "$bin/docker" "$bin/finspace-compose" "$bin/curl" "$bin/python3" "$bin/git"

printf 'postgres\nredis\nbackend\nfrontend\nsync-worker\ncategorization-prune\n' \
  >"$test_root/services"
cat >"$test_root/restored-probe.json" <<'JSON'
{
    "version": 1,
    "captured_at": "2026-09-03T00:00:00Z",
    "alembic_revision": "0017_gamma",
    "compared": {
        "workspaces": 1,
        "users": 2,
        "workspace_members": 2,
        "accounts_total": 5,
        "accounts_active": 4,
        "categories_total": 30,
        "categories_active": 29,
        "payees": 11,
        "transactions_total": 1200,
        "transactions_active": 1190,
        "transaction_splits": 40,
        "budget_periods": 6,
        "budget_allocations": 60,
        "goals": 3,
        "recurring_rules": 7,
        "import_batches": 9,
        "month_closures": 4,
        "google_sheet_bindings": 1,
        "google_connections": 0,
        "latest_transaction_occurred_at": "2026-09-01T10:00:00Z"
    },
    "informational": {
        "audit_log": 5000,
        "auth_sessions": 3,
        "sync_outbox": 0,
        "sync_inbox": 0
    }
}
JSON
cp "$test_root/restored-probe.json" "$test_root/source-probe.json"

# --- backup set fixtures --------------------------------------------------------------------------
build_set() {
  # build_set <set_id> <revision> <local_verified> [--corrupt-dump|--manifest-sha-disagrees]
  id="$1"
  revision="$2"
  verified="$3"
  variant="${4:-}"
  dir="$sets_root/$id"
  rm -rf -- "$dir"
  mkdir -p "$dir"
  printf 'pretend custom-format dump for %s' "$id" >"$dir/database.dump"
  sha=$(sha256sum "$dir/database.dump" | awk '{print $1}')
  manifest_sha="$sha"
  case "$variant" in
    --manifest-sha-disagrees)
      manifest_sha="0000000000000000000000000000000000000000000000000000000000000000"
      ;;
  esac
  cat >"$dir/database.manifest.json" <<EOF
{
  "filename": "finspace_${id}.dump",
  "sha256": "${manifest_sha}",
  "created_at": "2026-09-01T01:00:00Z",
  "database": "finspace",
  "alembic_revision": "${revision}",
  "format": "postgresql-custom",
  "size_bytes": 40
}
EOF
  cat >"$dir/backup-set.json" <<EOF
{
  "version": 1,
  "set_id": "${id}",
  "created_at": "2026-09-01T01:00:00Z",
  "finspace_commit": "abcdef1234567890abcdef1234567890abcdef12",
  "finspace_tag": "local-v0.16",
  "alembic_revision": "${revision}",
  "database": {
      "path": "database/finspace_${id}.dump",
      "manifest_path": "database/finspace_${id}.dump.manifest.json",
      "filename": "finspace_${id}.dump",
      "sha256": "${sha}",
      "manifest_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
      "size_bytes": 40,
      "manifest_size_bytes": 200
    },
  "n8n": {
      "included": false,
      "path": null,
      "sha256": null,
      "size_bytes": null
    }
}
EOF
  cat >"$dir/backup-set-report.json" <<EOF
{
  "version": 1,
  "set_id": "${id}",
  "created_at": "2026-09-01T01:00:00Z",
  "local_verified": ${verified},
  "local_verified_at": "2026-09-01T01:05:00Z",
  "offhost_verified": false,
  "offhost_verified_at": null,
  "offhost_destination_label": null,
  "error": null
}
EOF
  case "$variant" in
    --corrupt-dump) printf 'tampered' >>"$dir/database.dump" ;;
  esac
  printf '%s\n' "$dir"
}

good_set=$(build_set "2026-09-01T010000Z" "$target_head" true)
old_set=$(build_set "2026-09-02T010000Z" "0015_alpha" true)
unverified_set=$(build_set "2026-09-03T010000Z" "$target_head" false)
corrupt_set=$(build_set "2026-09-04T010000Z" "$target_head" true --corrupt-dump)
disagreeing_set=$(build_set "2026-09-05T010000Z" "$target_head" true --manifest-sha-disagrees)
future_set=$(build_set "2026-09-06T010000Z" "0018_future" true)

# --- runner ---------------------------------------------------------------------------------------
evidence_seq=0
last_evidence=""
last_output=""
last_status=0

run_drill() {
  evidence_seq=$((evidence_seq + 1))
  last_evidence="$test_root/evidence $evidence_seq.json"
  run_drill_with_evidence "$last_evidence" "$@"
}

# preflight is the only phase that can observe a bare host, so restore and verify are only
# meaningful as continuations of one drill. The chain helper models exactly that.
chain_evidence=""

start_chain() {
  evidence_seq=$((evidence_seq + 1))
  chain_evidence="$test_root/chain $evidence_seq.json"
  rm -f -- "$chain_evidence"
  run_drill_with_evidence "$chain_evidence" preflight --set-dir "$1" --confirm-clean-environment
  [ "$last_status" -eq 0 ] || fail "chain preflight failed: $last_output"
}

continue_chain() {
  run_drill_with_evidence "$chain_evidence" "$@"
}

run_drill_with_evidence() {
  evidence="$1"
  last_evidence="$evidence"
  shift
  : >"$test_root/docker.log"
  : >"$test_root/compose.log"
  : >"$test_root/git.log"
  last_status=0
  last_output=$(
    PATH="$bin:/usr/bin:/bin" \
    DOCKER_LOG="$test_root/docker.log" \
    COMPOSE_LOG="$test_root/compose.log" \
    GIT_LOG="$test_root/git.log" \
    STUB_PROJECT="$project" \
    STUB_TARGET_COMMIT="0123456789abcdef0123456789abcdef01234567" \
    STUB_TARGET_TAG="local-v0.16" \
    STUB_CONTAINERS="$test_root/containers" \
    STUB_VOLUMES="$test_root/volumes" \
    STUB_DB_STATE="$test_root/db-state" \
    STUB_SERVICES="$test_root/services" \
    STUB_RESTORED_PROBE="$test_root/restored-probe.json" \
    STUB_TARGET_HEAD="$target_head" \
    STUB_BACKUP_REVISION="${STUB_BACKUP_REVISION:-$target_head}" \
    STUB_RESTORE_FAILS="${STUB_RESTORE_FAILS:-false}" \
    STUB_PROBE_FAILS="${STUB_PROBE_FAILS:-false}" \
    STUB_HTTP_STATUS="${STUB_HTTP_STATUS:-200}" \
    STUB_TOPOLOGY_EXIT="${STUB_TOPOLOGY_EXIT:-0}" \
    "$drill" "$@" --project-root "$project" --evidence "$evidence" 2>&1
  ) || last_status=$?
}

evidence_field() {
  sed -n "s/.*\"$2\": *\"\\([^\"]*\\)\".*/\\1/p" "$1" | head -n 1
}
evidence_raw() {
  sed -n "s/.*\"$2\": *\\([^,]*\\),*\$/\\1/p" "$1" | head -n 1
}

reset_db_state() {
  printf 'tables=0\nrevision=\n' >"$test_root/db-state"
}
reset_db_state

# =================================================================================================
# 1. The script is executable and carries no destructive command
# =================================================================================================
[ -x "$drill" ] || fail "scripts/dr-restore-drill.sh is not executable in the checkout"
[ -x "$probe_script" ] || fail "scripts/dr-data-probe.sh is not executable in the checkout"

drill_body=$(grep -v '^[[:space:]]*#' "$drill")
for forbidden in 'down -v' 'volume rm' 'volume prune' 'system prune' 'image prune' \
  'reset --hard' 'clean -fdx' 'chmod -R' 'chown -R' 'safe.directory=*' 'dropdb' 'DROP DATABASE'; do
  assert_missing "$drill_body" "$forbidden" "the drill contains a forbidden command: $forbidden"
done
probe_body=$(grep -v '^[[:space:]]*#' "$probe_script")
for forbidden in 'INSERT' 'UPDATE ' 'DELETE' 'DROP' 'ALTER'; do
  assert_missing "$probe_body" "$forbidden" "the data probe is not read-only: $forbidden"
done
assert_contains "$probe_body" "READ ONLY" "the data probe does not open a read-only transaction"
for leaky in description comment counterparty email external_id memo; do
  assert_missing "$probe_body" "$leaky" "the data probe reads a free-text column: $leaky"
done

# =================================================================================================
# 2. Confirmation is mandatory, and refusal happens before anything is inspected
# =================================================================================================
run_drill preflight --set-dir "$good_set"
assert_equal "2" "$last_status" "preflight without confirmation: exit code"
assert_contains "$last_output" "confirm-clean-environment" "no explanation of the missing flag"
[ ! -e "$last_evidence" ] || fail "an unconfirmed run still wrote an evidence file"

# =================================================================================================
# 3. Clean-host proof
# =================================================================================================
printf 'finspace-backend-1\nfinspace-postgres-1\n' >"$test_root/containers"
run_drill preflight --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "existing containers: exit code"
assert_contains "$last_output" "existing_finspace_environment_detected" "containers were not refused"
[ -s "$last_evidence" ] || fail "a refused run left no evidence"
assert_equal "FAILED" "$(evidence_field "$last_evidence" verdict)" "verdict for a dirty host"
assert_equal "existing_finspace_environment_detected" \
  "$(evidence_field "$last_evidence" failure_reason)" "failure reason for a dirty host"
: >"$test_root/containers"

printf 'finspace_postgres_data\nunrelated_volume\n' >"$test_root/volumes"
run_drill preflight --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "existing volume: exit code"
assert_contains "$last_output" "existing_finspace_environment_detected" "the volume was not refused"

# An unrelated Docker host is not the assertion: only Finspace state is.
printf 'someone_elses_volume\npostgres_data_of_another_app\n' >"$test_root/volumes"
run_drill preflight --set-dir "$good_set" --confirm-clean-environment
assert_equal "0" "$last_status" "unrelated Docker resources must not block the drill"
assert_equal "PREFLIGHT_PASSED" "$(evidence_field "$last_evidence" verdict)" "clean host verdict"

# The documented escape is recorded, so it can never be mistaken for clean-host acceptance.
printf 'finspace_postgres_data\n' >"$test_root/volumes"
run_drill preflight --set-dir "$good_set" --confirm-clean-environment --isolated-test-mode
assert_equal "0" "$last_status" "isolated test mode: exit code"
assert_equal "true" "$(evidence_raw "$last_evidence" isolated_test_mode)" "isolated flag not recorded"
assert_equal "false" "$(evidence_raw "$last_evidence" clean_host_proven)" \
  "isolated mode must not claim a clean host"
: >"$test_root/volumes"

# =================================================================================================
# 4. Backup set validation
# =================================================================================================
run_drill preflight --set-dir "$good_set" --confirm-clean-environment
assert_equal "0" "$last_status" "a good set failed preflight"
evidence="$last_evidence"
assert_equal "PREFLIGHT_PASSED" "$(evidence_field "$evidence" verdict)" "preflight verdict"
assert_equal "2026-09-01T010000Z" "$(evidence_field "$evidence" set_id)" "set id"
assert_equal "finspace_2026-09-01T010000Z.dump" "$(evidence_field "$evidence" dump_filename)" "dump filename"
assert_equal "$target_head" "$(evidence_field "$evidence" alembic_revision)" "backup revision"
assert_equal "local-v0.16" "$(evidence_field "$evidence" finspace_tag)" "backup tag"
assert_equal "true" "$(evidence_raw "$evidence" local_verified)" "local_verified"
assert_equal "false" "$(evidence_raw "$evidence" offhost_verified)" "offhost_verified"
assert_equal "not_tested" "$(evidence_field "$evidence" n8n_restore)" "n8n default"
assert_contains "$(cat "$evidence")" '"login": "pending"' "operator login must stay pending"

assert_refusal() {
  label="$1"
  expected="$2"
  shift 2
  run_drill preflight --set-dir "$1" --confirm-clean-environment
  assert_equal "1" "$last_status" "$label: exit code"
  assert_contains "$last_output" "$expected" "$label: expected reason $expected"
  assert_equal "FAILED" "$(evidence_field "$last_evidence" verdict)" "$label: verdict"
}

assert_refusal "an unverified set" "backup_set_not_locally_verified" "$unverified_set"
assert_refusal "a corrupted dump" "dump_sha256_mismatch" "$corrupt_set"
assert_refusal "disagreeing manifests" "set_and_dump_manifest_disagree" "$disagreeing_set"

rm -rf -- "$test_root/broken"
mkdir -p "$test_root/broken/2026-09-07T010000Z"
assert_refusal "an empty set directory" "set_manifest_missing" "$test_root/broken/2026-09-07T010000Z"

# =================================================================================================
# 5. Release compatibility
# =================================================================================================
run_drill preflight --set-dir "$old_set" --confirm-clean-environment
assert_equal "0" "$last_status" "an older but known revision must be accepted"
assert_equal "B" "$(evidence_field "$last_evidence" case)" "case for an older backup"
assert_equal "restore_then_forward_migrate" "$(evidence_field "$last_evidence" decision)" "case B decision"
assert_equal "$target_head" "$(evidence_field "$last_evidence" alembic_head)" "target head"

run_drill preflight --set-dir "$future_set" --confirm-clean-environment
assert_equal "1" "$last_status" "a newer backup revision must be refused"
assert_contains "$last_output" "backup_revision_newer_than_target_release" "case C was not refused"
assert_equal "C" "$(evidence_field "$last_evidence" case)" "case C recorded"
assert_equal "refuse" "$(evidence_field "$last_evidence" decision)" "case C decision"
assert_missing "$last_output" "downgrade" "the drill must never suggest a downgrade"

# =================================================================================================
# 6. Restore
# =================================================================================================
# A restore that was never preflighted is refused: the clean-host proof cannot be back-dated.
reset_db_state
run_drill restore --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "restore without preflight: exit code"
assert_contains "$last_output" "phase_preflight_has_not_completed" "restore ran without a preflight"
assert_missing "$(cat "$test_root/compose.log")" "restore.sh" "restore.sh ran without a preflight"

start_chain "$good_set"
printf 'tables=57\nrevision=0017_gamma\n' >"$test_root/db-state"
continue_chain restore --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "a populated target database must be refused"
assert_contains "$last_output" "target_database_is_not_empty" "a populated database was not refused"
assert_missing "$(cat "$test_root/compose.log")" "restore.sh" "restore.sh ran against a populated database"
assert_equal "57" "$(evidence_raw "$last_evidence" target_tables_before)" "recorded table count"

start_chain "$good_set"
reset_db_state
continue_chain restore --set-dir "$good_set" --confirm-clean-environment
assert_equal "0" "$last_status" "a clean restore failed: $last_output"
compose_log=$(cat "$test_root/compose.log")
assert_contains "$compose_log" "restore.sh" "restore.sh was never invoked"
assert_contains "$compose_log" "--overwrite-main" "restore was not run against the fresh main database"
assert_contains "$compose_log" "/backups/database/finspace_2026-09-01T010000Z.dump" "wrong dump path"
assert_missing "$compose_log" "alembic upgrade" "case A must not run a migration"
assert_equal "RESTORE_PASSED" "$(evidence_field "$last_evidence" verdict)" "restore verdict"
assert_equal "succeeded" "$(evidence_field "$last_evidence" result)" "restore result"
assert_equal "not_required" "$(evidence_field "$last_evidence" migration)" "case A migration"
[ -s "$project/backups/database/finspace_2026-09-01T010000Z.dump" ] || fail "the dump was not staged"
[ -s "$project/backups/database/finspace_2026-09-01T010000Z.dump.manifest.json" ] ||
  fail "the dump manifest was not staged"

# An artifact already sitting at the staging path is reused rather than re-copied, so it has to be
# re-digested: a stale or tampered file there must never be restored.
start_chain "$good_set"
reset_db_state
printf 'not the dump you think it is' >"$project/backups/database/finspace_2026-09-01T010000Z.dump"
continue_chain restore --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "a stale staged dump must be refused"
assert_contains "$last_output" "staged_dump_sha256_mismatch" "the stale staged dump was accepted"
assert_missing "$(cat "$test_root/compose.log")" "restore.sh" "restore ran against a stale dump"
rm -f -- "$project/backups/database/finspace_2026-09-01T010000Z.dump"

# The database that comes back must be the one the backup said it was.
start_chain "$good_set"
reset_db_state
STUB_BACKUP_REVISION="0016_beta" continue_chain restore --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "a restored revision unlike the backup's must fail"
assert_contains "$last_output" "restored_revision_does_not_match_backup" \
  "a restored database at the wrong revision was accepted"

# Case B restores first and only then migrates forward, never the other way round.
start_chain "$old_set"
reset_db_state
STUB_BACKUP_REVISION="0015_alpha" continue_chain restore --set-dir "$old_set" --confirm-clean-environment
assert_equal "0" "$last_status" "case B restore failed: $last_output"
compose_log=$(cat "$test_root/compose.log")
assert_contains "$compose_log" "alembic upgrade head" "case B did not migrate forward"
restore_line=$(printf '%s\n' "$compose_log" | grep -n 'restore.sh' | head -n 1 | cut -d: -f1)
migrate_line=$(printf '%s\n' "$compose_log" | grep -n 'alembic upgrade' | head -n 1 | cut -d: -f1)
[ "$restore_line" -lt "$migrate_line" ] || fail "the migration ran before the restore"
assert_equal "applied" "$(evidence_field "$last_evidence" migration)" "case B migration"
assert_equal "$target_head" "$(evidence_field "$last_evidence" revision_after_migration)" "revision after migration"

# A failing restore is recorded as a failure, not swallowed.
start_chain "$good_set"
reset_db_state
STUB_RESTORE_FAILS=true continue_chain restore --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "a failing restore must exit non-zero"
assert_equal "FAILED" "$(evidence_field "$last_evidence" verdict)" "failed restore verdict"
assert_equal "restore_failed" "$(evidence_field "$last_evidence" failure_reason)" "failed restore reason"

# =================================================================================================
# 7. Verify
# =================================================================================================
# Verification only makes sense as the third step of one drill, so the chain is completed first.
reset_db_state
run_drill verify --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "verify without a restore: exit code"
assert_contains "$last_output" "phase_restore_has_not_completed" "verify ran without a restore"

start_chain "$good_set"
reset_db_state
continue_chain restore --set-dir "$good_set" --confirm-clean-environment
[ "$last_status" -eq 0 ] || fail "chain restore failed: $last_output"

printf 'tables=42\nrevision=%s\n' "$target_head" >"$test_root/db-state"
continue_chain verify --set-dir "$good_set" --confirm-clean-environment \
  --source-probe "$test_root/source-probe.json"
assert_equal "0" "$last_status" "verify failed on a healthy system: $last_output"
evidence="$last_evidence"
assert_equal "VERIFY_PASSED" "$(evidence_field "$evidence" verdict)" "verify verdict"
assert_equal "PASS" "$(evidence_field "$evidence" topology)" "topology result"
assert_equal "match" "$(evidence_field "$evidence" data_probe_comparison)" "probe comparison"
assert_equal "200" "$(evidence_field "$evidence" backend_ready)" "backend readiness"
assert_contains "$(cat "$evidence")" '"transactions_active": 1190' "the restored probe was not inlined"
assert_contains "$last_output" "operator login" "verify must not present itself as acceptance"

# A real difference in financial state is a failure, not a warning.
sed 's/"transactions_active": 1190/"transactions_active": 1189/' "$test_root/source-probe.json" \
  >"$test_root/drifted-probe.json"
continue_chain verify --set-dir "$good_set" --confirm-clean-environment \
  --source-probe "$test_root/drifted-probe.json"
assert_equal "1" "$last_status" "a probe mismatch must fail"
assert_equal "data_probe_mismatch" "$(evidence_field "$last_evidence" failure_reason)" "mismatch reason"
assert_contains "$(evidence_field "$last_evidence" data_probe_mismatches)" "transactions_active" \
  "the mismatching key was not named"

# Without a source probe the drill says so rather than implying a comparison happened.
continue_chain verify --set-dir "$good_set" --confirm-clean-environment
assert_equal "0" "$last_status" "verify without a source probe failed"
assert_equal "not_compared" "$(evidence_field "$last_evidence" data_probe_comparison)" \
  "a missing source probe must be reported as not_compared"

# A service that is down fails the drill.
printf 'postgres\nredis\nbackend\nfrontend\n' >"$test_root/services"
continue_chain verify --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "a stopped worker must fail verification"
assert_equal "service_not_running" "$(evidence_field "$last_evidence" failure_reason)" "stopped worker reason"
printf 'postgres\nredis\nbackend\nfrontend\nsync-worker\ncategorization-prune\n' \
  >"$test_root/services"

# The schema must be the target head, not merely "some revision".
printf 'tables=42\nrevision=0016_beta\n' >"$test_root/db-state"
continue_chain verify --set-dir "$good_set" --confirm-clean-environment
assert_equal "1" "$last_status" "a wrong revision must fail verification"
assert_equal "alembic_revision_is_not_target_head" "$(evidence_field "$last_evidence" failure_reason)" \
  "wrong revision reason"
printf 'tables=42\nrevision=%s\n' "$target_head" >"$test_root/db-state"

# =================================================================================================
# 8. Candidates displays, never selects
# =================================================================================================
mkdir -p "$test_root/local/sets"
cp -r "$good_set" "$test_root/local/sets/"
cp -r "$unverified_set" "$test_root/local/sets/"
listing=$(PATH="$bin:/usr/bin:/bin" "$drill" candidates --backup-root "$test_root/local" 2>&1)
assert_contains "$listing" "2026-09-01T010000Z" "the verified set was not listed"
assert_contains "$listing" "2026-09-03T010000Z" "the unverified set was not listed"
assert_contains "$listing" "explicitly" "candidates must state that selection is manual"

# =================================================================================================
# 9. No secret leakage, and nothing the drill did not create is removed
# =================================================================================================
reset_db_state
run_drill preflight --set-dir "$good_set" --confirm-clean-environment
assert_missing "$last_output" "$planted_secret" "a secret reached the drill output"
assert_missing "$(cat "$last_evidence")" "$planted_secret" "a secret reached the evidence artifact"
for leaky in JWT_SECRET_KEY POSTGRES_PASSWORD ENCRYPTION_KEY CLIENT_SECRET password; do
  assert_missing "$(cat "$last_evidence")" "$leaky" "the evidence mentions $leaky"
done
[ -f "$project/.env" ] || fail "the drill removed the operator's .env"
[ -f "$project/.operator-file" ] || fail "the drill removed an unrelated operator file"
[ -f "$project/backups/database/.keep-me" ] || fail "the drill removed an unrelated backup file"
[ -f "$good_set/database.dump" ] || fail "the drill removed the source backup artifact"
# Whitelist rather than blacklist: the drill talks to docker only to look, so every invocation it
# ever makes must be one of two read-only subcommands.
while IFS= read -r invocation; do
  [ -n "$invocation" ] || continue
  case "$invocation" in
    "docker ps -a "*|"docker volume ls "*) ;;
    *) fail "the drill ran a docker command that is not read-only: $invocation" ;;
  esac
done <"$test_root/docker.log"

# =================================================================================================
# 10. The artifact is valid JSON
# =================================================================================================
if command -v python3 >/dev/null 2>&1 && [ ! -x "$bin/python3" ]; then
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$last_evidence" ||
    fail "the evidence artifact is not valid JSON"
elif command -v /usr/bin/python3 >/dev/null 2>&1; then
  /usr/bin/python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$last_evidence" ||
    fail "the evidence artifact is not valid JSON"
else
  printf 'dr-restore-drill test: note: no python3, JSON was not parsed\n'
fi

printf 'dr-restore-drill test: PASS\n'
