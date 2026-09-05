#!/bin/sh
# The release-candidate gate decides whether one commit may become Finspace 1.0, so the properties
# worth testing are its judgement, not the gates it calls. Migrations, performance, topology and
# documentation each have their own suite; re-running them here would prove nothing and take twenty
# minutes. What is proved here instead: the candidate it accepts and refuses, the order it runs in,
# where it stops early, how it distinguishes FAIL from BLOCKED, what its exit codes mean, and that
# it never issues a destructive command.
#
# The heavy phases are therefore stubbed. A separate end-to-end invocation against the real
# repository is part of the release procedure and is not automated here.
set -eu

fail() {
  printf 'release-candidate-gate test: FAIL: %s\n' "$1" >&2
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

assert_status() {
  [ "$1" -eq "$2" ] || fail "$3 (expected exit $2, got $1)"
}

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
gate_source="$repository_root/scripts/release-candidate-gate.sh"
[ -f "$gate_source" ] || fail "the gate is missing: $gate_source"

python_command="${FINSPACE_PYTHON:-python3}"
command -v "$python_command" >/dev/null 2>&1 || fail "$python_command is required for this suite"
command -v git >/dev/null 2>&1 || fail "git is required for this suite"

test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

project="$test_root/checkout"
mkdir -p "$project/scripts/tests" "$project/backend/scripts" "$project/docs"

# Evidence and reports are written outside the checkout on purpose: a gate that dirtied the tree it
# is about to certify would fail its own next run.
acceptance="$test_root/acceptance"
mkdir -p "$acceptance"

# ---------------------------------------------------------------------------------------------
# A checkout that looks like Finspace to the gate: the real orchestrator, the real evidence
# validator and the real strict-status helper, with the expensive gates replaced by switches.
# ---------------------------------------------------------------------------------------------

cp "$gate_source" "$project/scripts/release-candidate-gate.sh"
cp "$repository_root/scripts/git-status-strict.sh" "$project/scripts/git-status-strict.sh"
cp "$repository_root/backend/scripts/release_evidence.py" "$project/backend/scripts/"

gate="$project/scripts/release-candidate-gate.sh"

cat >"$project/backend/scripts/validate_compose_topology.py" <<'STUB'
import os
import sys

sys.exit(1 if os.environ.get("RC_TOPOLOGY_FAIL") else 0)
STUB

cat >"$project/backend/scripts/validate_docs.py" <<'STUB'
import os
import sys

sys.exit(1 if os.environ.get("RC_DOCS_FAIL") else 0)
STUB

# Stands in for the production wrapper: the gate must reach the merged production configuration
# through it rather than assembling -f flags of its own.
cat >"$project/scripts/finspace-compose.sh" <<'STUB'
#!/bin/sh
printf 'wrapper %s root=%s\n' "$*" "${FINSPACE_PROJECT_ROOT:-unset}" >>"$RC_COMPOSE_LOG"
[ -z "${RC_WRAPPER_FAIL:-}" ] || exit 1
exit 0
STUB

# Records every compose invocation and fails on demand, so the test can assert what the gate asked
# for without any container existing.
cat >"$test_root/compose-stub.sh" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$RC_COMPOSE_LOG"
for token in ${RC_COMPOSE_FAIL:-}; do
  case "$*" in
    *"$token"*) exit 1 ;;
  esac
done
case "$*" in
  *test_runner.py*)
    printf '%s\n' "${RC_PYTEST_SUMMARY:-=== 470 passed, 1 skipped in 61.00s ===}"
    ;;
esac
exit 0
STUB

cat >"$project/scripts/tests/example.test.sh" <<'STUB'
#!/bin/sh
[ -z "${RC_SHELL_SUITE_FAIL:-}" ] || exit 1
echo "example test: PASS"
STUB

# Exit 4 is the repository's "this environment cannot run me" code, used by the root-only runtime
# storage integration test. It must not fail the release, and it must not vanish either.
cat >"$project/scripts/tests/environment.test.sh" <<'STUB'
#!/bin/sh
[ -n "${RC_SHELL_SUITE_SKIP:-}" ] || exit 0
echo "environment test: SKIP: needs root"
exit 4
STUB

printf 'DATABASE_URL=postgresql+asyncpg://user@postgres/finspace\n' >"$project/.env"
printf 'name: finspace\n' >"$project/docker-compose.yml"
printf 'services: {}\n' >"$project/compose.production.yml"
printf '# Finspace test checkout\n' >"$project/README.md"

# The frontend checks are host commands, so npm is stubbed on PATH rather than in a container.
# node_modules is present throughout, because the interesting question is whether an already
# installed tree is allowed to stand in for the candidate's lockfile. It must not be.
mkdir -p "$test_root/bin" "$project/frontend/node_modules"
printf '{ "name": "finspace-frontend" }\n' >"$project/frontend/package.json"
cat >"$test_root/bin/npm" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$RC_NPM_LOG"
for token in ${RC_NPM_FAIL:-}; do
  case "$*" in
    *"$token"*) exit 1 ;;
  esac
done
exit 0
STUB
chmod +x "$test_root/bin/npm"
npm_log="$test_root/npm.log"
RC_NPM_LOG="$npm_log"
PATH="$test_root/bin:$PATH"
export RC_NPM_LOG PATH

(
  cd "$project"
  git init --quiet
  git config user.email "test@example.invalid"
  git config user.name "Release Gate Test"
  git config core.autocrlf false
  git config commit.gpgsign false
  git add -A
  git commit --quiet -m "test checkout"
) || fail "could not build the test checkout"

candidate=$(git -C "$project" rev-parse HEAD)
head_revision="0017_categorization_history"
head_count=17

compose_log="$test_root/compose.log"

# Runs the gate with everything the test controls reset, then leaves $status and $output behind.
run_gate() {
  : >"$compose_log"
  : >"$npm_log"
  status=0
  output=$(
    cd "$project" &&
    RC_COMPOSE_LOG="$compose_log" \
    FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" \
    FINSPACE_PYTHON="$python_command" \
    sh "$gate" --project-root "$project" \
      --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" "$@" 2>&1
  ) || status=$?
}

run_valid() {
  run_gate --candidate "$candidate" "$@"
}

# ---------------------------------------------------------------------------------------------
# Invocation errors: exit 2, before anything is executed
# ---------------------------------------------------------------------------------------------

run_gate --allow-pending-operational
assert_status "$status" 2 "a missing candidate was accepted"

run_gate --candidate "6604960" --allow-pending-operational
assert_status "$status" 2 "an abbreviated candidate was accepted"
assert_contains "$output" "40" "the diagnostic did not explain the candidate format"

run_gate --candidate "ZZZ4960da05fea328bfafbfc4b67b6ecb03dcc91" --allow-pending-operational
assert_status "$status" 2 "a non-hexadecimal candidate was accepted"

run_gate --candidate "$(printf '%s' "$candidate" | tr 'a-f' 'A-F')" --allow-pending-operational
assert_status "$status" 2 "an uppercase candidate was accepted"

status=0
output=$(sh "$gate" --candidate "$candidate" --expect-alembic-head x \
  --expect-alembic-count seventeen --allow-pending-operational 2>&1) || status=$?
assert_status "$status" 2 "a non-numeric revision count was accepted"

run_gate --candidate "$candidate" --allow-pending-operational --unknown-flag
assert_status "$status" 2 "an unknown argument was accepted"

status=0
output=$(sh "$gate" --candidate "$candidate" --expect-alembic-head "$head_revision" \
  --expect-alembic-count "$head_count" --project-root relative/path \
  --allow-pending-operational 2>&1) || status=$?
assert_status "$status" 2 "a relative project root was accepted"

# Asking for a release decision while supplying nothing to decide on is a mistake in the call.
run_valid
assert_status "$status" 2 "a release decision was attempted with no acceptance evidence"
assert_contains "$output" "allow-pending-operational" "the diagnostic did not name the escape hatch"
[ ! -s "$compose_log" ] || fail "phases ran before the invocation was validated"

# The pins carry the whole point of a release gate and must not be optional.
status=0
output=$(sh "$gate" --candidate "$candidate" --allow-pending-operational 2>&1) || status=$?
assert_status "$status" 2 "the Alembic expectations were optional"

# ---------------------------------------------------------------------------------------------
# Candidate identity: fail fast, exit 1
# ---------------------------------------------------------------------------------------------

wrong_candidate="0000000000000000000000000000000000000000"
run_gate --candidate "$wrong_candidate" --allow-pending-operational
assert_status "$status" 1 "a checkout that is not the candidate was accepted"
assert_contains "$output" "candidate-identity" "the failing phase was not named"
assert_missing "$output" "migration-gate" "the gate continued past a wrong candidate"
[ ! -s "$compose_log" ] || fail "a wrong candidate still reached the container phases"

printf 'uncommitted\n' >"$project/dirty.txt"
run_valid --allow-pending-operational
assert_status "$status" 1 "a dirty worktree was accepted"
assert_contains "$output" "dirty.txt" "the diagnostic did not name the uncommitted file"
rm -f "$project/dirty.txt"

mv "$project/.env" "$test_root/env.saved"
run_valid --allow-pending-operational
assert_status "$status" 1 "a checkout without .env was accepted"
mv "$test_root/env.saved" "$project/.env"

# ---------------------------------------------------------------------------------------------
# The engineering suite with operational acceptance still pending
# ---------------------------------------------------------------------------------------------

evidence_json="$test_root/rc.json"
run_valid --allow-pending-operational --json-output "$evidence_json"
assert_status "$status" 3 "an engineering-only run did not report BLOCKED"
assert_contains "$output" "ENGINEERING STATUS: PASS" "the engineering result was not stated"
assert_contains "$output" "RELEASE STATUS: BLOCKED" "the release result was not stated"
assert_contains "$output" "F003" "F003 was not listed as a blocker"
assert_contains "$output" "F004" "F004 was not listed as a blocker"
assert_missing "$output" "RELEASE STATUS: PASS" "an engineering-only run claimed release approval"

# Every subject the release depends on must actually be represented as a phase.
for phase in candidate-identity compose-topology images-development migration-gate backend-static \
  backend-tests performance-smoke frontend-tests images-release shell-tests docs-gate \
  operational-acceptance; do
  assert_contains "$output" "$phase" "the run has no $phase phase"
done

log_text=$(cat "$compose_log")
assert_contains "$log_text" "validate_migrations.py --expect-head $head_revision --expect-count $head_count" \
  "F008 was not invoked with the reviewed expectations"
assert_contains "$log_text" "performance_smoke.py" "F014 was not invoked"
assert_contains "$log_text" "test_runner.py" "the backend suite was not run through its own runner"
assert_contains "$log_text" "compose.production.yml" "the production overlay was never built"
assert_contains "$log_text" "wrapper config --quiet" "the merged production config skipped the wrapper"
assert_contains "$log_text" "run --rm --no-deps" "container work did not use a fresh container"
assert_missing "$log_text" "exec " "the gate entered a container it did not create"

npm_text=$(cat "$npm_log")
for command in "ci" "test" "run typecheck" "run lint"; do
  assert_contains "$npm_text" "$command" "the frontend checks skipped: npm $command"
done
# An existing node_modules says nothing about which lockfile produced it, so it must not be
# allowed to stand in for the candidate's dependency tree.
[ -d "$project/frontend/node_modules" ] || fail "the fixture lost its installed dependency tree"
assert_contains "$npm_text" "ci" "an existing node_modules suppressed the lockfile install"
case "$npm_text" in
  ci*) ;;
  *) fail "the dependency install did not come first" ;;
esac

# A release gate must never mutate anything it is measuring.
for forbidden in "--apply" "downgrade" "down -v" "restore" "prune" "DROP DATABASE"; do
  assert_missing "$log_text" "$forbidden" "the gate issued a destructive command: $forbidden"
done
gate_text=$(cat "$gate")
for forbidden in "rm -rf /" "docker volume rm" "docker system prune" "alembic downgrade" \
  "--apply" "chmod -R 777" "git reset --hard" "git clean -fdx"; do
  assert_missing "$gate_text" "$forbidden" "the gate's source contains: $forbidden"
done
assert_missing "$gate_text" "eval " "the gate uses eval"

# --- the evidence document ---------------------------------------------------------------------
[ -f "$evidence_json" ] || fail "no evidence document was written"
[ ! -e "$evidence_json.partial" ] || fail "a partial evidence file was left behind"
"$python_command" - "$evidence_json" <<'PY' || fail "the evidence document is not usable"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)

assert document["version"] == 1, document["version"]
assert document["release_status"] == "blocked", document["release_status"]
assert document["engineering_status"] == "pass", document["engineering_status"]
assert document["operational_status"] == "pending", document["operational_status"]
assert len(document["phases"]) == 12, document["phases"]
assert all(phase["status"] == "pass" for phase in document["phases"]), document["phases"]
assert document["blockers"], "a blocked release recorded no blockers"
assert document["known_limitations"], "known limitations were not published"
assert len(document["expected_alembic_head"]) > 0
serialised = json.dumps(document).lower()
for forbidden in ("password", "secret", "jwt", "postgresql://", "token"):
    assert forbidden not in serialised, forbidden
PY

# ---------------------------------------------------------------------------------------------
# Phase failures
# ---------------------------------------------------------------------------------------------

# Topology is fail-fast: nothing after it should have run.
status=0
output=$(
  cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_TOPOLOGY_FAIL=1 \
  FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command" \
  sh "$gate" --project-root "$project" --candidate "$candidate" \
    --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" \
    --allow-pending-operational 2>&1
) || status=$?
assert_status "$status" 1 "an invalid topology did not fail the release"
assert_contains "$output" "RELEASE STATUS: FAIL" "an invalid topology did not produce FAIL"
assert_missing "$output" "backend-tests" "the gate continued past an invalid topology"

# The independent quality gates are collected rather than fail-fast: one failure must not hide the
# rest of the picture.
status=0
output=$(
  cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_SHELL_SUITE_FAIL=1 \
  FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command" \
  sh "$gate" --project-root "$project" --candidate "$candidate" \
    --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" \
    --allow-pending-operational 2>&1
) || status=$?
assert_status "$status" 1 "a failing shell suite did not fail the release"
assert_contains "$output" "docs-gate" "a later independent phase was skipped after one failure"

# A suite that cannot run here is reported by name, not folded into the passing total and not
# treated as a defect.
status=0
output=$(
  cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_SHELL_SUITE_SKIP=1   FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command"   sh "$gate" --project-root "$project" --candidate "$candidate"     --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count"     --allow-pending-operational 2>&1
) || status=$?
assert_status "$status" 3 "a suite that cannot run in this environment failed the engineering run"
assert_contains "$output" "environment.test.sh" "the skipped suite was not named"
assert_missing "$output" "all passed" "a skipped suite was counted as a pass"
assert_contains "$output" "RELEASE STATUS: BLOCKED" "the engineering run did not report BLOCKED"
assert_missing "$output" "RELEASE STATUS: PASS" "a skipped suite still reached a release approval"

run_failing_compose() {
  status=0
  output=$(
    cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_COMPOSE_FAIL="$1" \
    FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command" \
    sh "$gate" --project-root "$project" --candidate "$candidate" \
      --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" \
      --allow-pending-operational 2>&1
  ) || status=$?
}

run_failing_compose "validate_migrations.py"
assert_status "$status" 1 "a failing migration gate did not fail the release"
assert_missing "$output" "backend-tests" "the gate continued past a failing migration gate"

run_failing_compose "performance_smoke.py"
assert_status "$status" 1 "a failing performance gate did not fail the release"

run_failing_compose "compose.production.yml"
assert_status "$status" 1 "a failing production build did not fail the release"
assert_contains "$output" "images-release" "the failing build phase was not named"

run_failing_npm() {
  status=0
  output=$(
    cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_NPM_FAIL="$1" \
    FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command" \
    sh "$gate" --project-root "$project" --candidate "$candidate" \
      --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" \
      --allow-pending-operational 2>&1
  ) || status=$?
}

for failing in ci test typecheck lint; do
  run_failing_npm "$failing"
  assert_status "$status" 1 "a failing frontend $failing did not fail the release"
done

# A checkout without an installed dependency tree behaves identically: install, then check.
rm -rf "$project/frontend/node_modules"
run_valid --allow-pending-operational
assert_status "$status" 3 "the frontend phase failed on a fresh checkout"
assert_contains "$(cat "$npm_log")" "ci" "a fresh checkout did not install from the lockfile"
mkdir -p "$project/frontend/node_modules"

run_failing_compose "mypy"
assert_status "$status" 1 "a failing static check did not fail the release"

run_pytest_summary() {
  status=0
  output=$(
    cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_PYTEST_SUMMARY="$1" \
    FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command" \
    sh "$gate" --project-root "$project" --candidate "$candidate" \
      --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" \
      --allow-pending-operational ${2:+--allow-skips "$2"} 2>&1
  ) || status=$?
}

# A green exit code from pytest is not the same as a green suite: the summary is read as well.
run_pytest_summary "=== 468 passed, 2 failed, 1 skipped in 61.00s ==="
assert_status "$status" 1 "failing tests were reported as a passing suite"

run_pytest_summary "=== 468 passed, 2 error in 61.00s ==="
assert_status "$status" 1 "test errors were reported as a passing suite"

run_pytest_summary "=== 400 passed, 70 deselected in 61.00s ==="
assert_status "$status" 1 "deselected tests were accepted in a release run"
assert_contains "$output" "deselected" "the diagnostic did not mention deselection"

run_pytest_summary "=== 460 passed, 11 skipped in 61.00s ==="
assert_status "$status" 1 "an unexplained pile of skipped tests was accepted"
run_pytest_summary "=== 460 passed, 11 skipped in 61.00s ===" 11
assert_status "$status" 3 "an explicitly allowed skip count was still refused"

run_pytest_summary "no summary here"
assert_status "$status" 1 "a missing pytest summary was accepted"

status=0
output=$(
  cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_DOCS_FAIL=1 \
  FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command" \
  sh "$gate" --project-root "$project" --candidate "$candidate" \
    --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" \
    --allow-pending-operational 2>&1
) || status=$?
assert_status "$status" 1 "a failing documentation gate did not fail the release"

# Discovery must not be able to pass by finding nothing. This needs its own checkout: deleting a
# tracked file in the main one would simply make the worktree dirty and never reach the phase.
empty_project="$test_root/checkout-empty"
cp -R "$project" "$empty_project"
rm -f "$empty_project"/scripts/tests/*.test.sh
(
  cd "$empty_project"
  git add -A
  git commit --quiet -m "a checkout with no shell suites"
) || fail "could not build the empty-suite checkout"
empty_candidate=$(git -C "$empty_project" rev-parse HEAD)

status=0
output=$(
  cd "$empty_project" && RC_COMPOSE_LOG="$compose_log"   FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command"   sh "$empty_project/scripts/release-candidate-gate.sh" --project-root "$empty_project"     --candidate "$empty_candidate" --expect-alembic-head "$head_revision"     --expect-alembic-count "$head_count" --allow-pending-operational 2>&1
) || status=$?
assert_status "$status" 1 "an empty shell test directory was reported as a pass"
assert_contains "$output" "discovered" "the empty suite directory was not explained"

# A failed run still publishes evidence, and it still must not be a truncated file.
failed_json="$test_root/failed.json"
status=0
output=$(
  cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_DOCS_FAIL=1 \
  FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command" \
  sh "$gate" --project-root "$project" --candidate "$candidate" \
    --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" \
    --allow-pending-operational --json-output "$failed_json" 2>&1
) || status=$?
assert_status "$status" 1 "a failing documentation gate did not fail the release"
[ ! -e "$failed_json.partial" ] || fail "a failed run left a partial evidence file"
"$python_command" - "$failed_json" <<'PY' || fail "the failed run's evidence is not usable"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
assert document["release_status"] == "fail", document["release_status"]
assert document["engineering_status"] == "fail", document["engineering_status"]
failures = [phase["name"] for phase in document["phases"] if phase["status"] == "fail"]
assert failures == ["docs-gate"], failures
PY

# ---------------------------------------------------------------------------------------------
# Operational acceptance
# ---------------------------------------------------------------------------------------------

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
offhost="$acceptance/f003.json"
restore="$acceptance/f004.json"
list="$acceptance/checklist.json"

cat >"$offhost" <<JSON
{
  "version": 1,
  "acceptance": "F003",
  "candidate": "$candidate",
  "accepted_at": "$now",
  "set_id": "2026-09-01T010000Z",
  "dump_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "alembic_revision": "$head_revision",
  "finspace_commit": "$candidate",
  "local_verified": true,
  "offhost_verified": true,
  "offhost_verified_at": "$now",
  "offhost_destination_label": "homelab-backup",
  "separate_failure_domain": true,
  "remote_sha256_verified": true
}
JSON

cat >"$restore" <<JSON
{
  "version": 1,
  "acceptance": "F004",
  "candidate": "$candidate",
  "accepted_at": "$now",
  "drill_id": "drill-001",
  "verdict": "PASSED",
  "clean_host_proven": true,
  "isolated_test_mode": false,
  "restore_result": "restored",
  "data_probe_comparison": "match",
  "operator_login": "ok",
  "operator_ui_data_review": "ok",
  "target_commit": "$candidate",
  "target_alembic_head": "$head_revision",
  "backup_alembic_revision": "$head_revision",
  "compatibility_case": "same",
  "candidate_relation": "same-commit"
}
JSON

cat >"$list" <<JSON
{
  "version": 1,
  "acceptance": "release-checklist",
  "candidate": "$candidate",
  "acknowledged_at": "$now",
  "open_p0_p1": 0,
  "clean_server_install_verified": true,
  "production_acceptance_verified": true
}
JSON

run_valid --offhost-evidence "$offhost"
assert_status "$status" 3 "F003 alone was enough to release"
assert_contains "$output" "F004" "the missing restore acceptance was not named"

run_valid --restore-evidence "$restore"
assert_status "$status" 3 "F004 alone was enough to release"
assert_contains "$output" "F003" "the missing off-host acceptance was not named"

run_valid --offhost-evidence "$offhost" --restore-evidence "$restore"
assert_status "$status" 3 "a release without the checklist was approved"
assert_contains "$output" "checklist" "the missing checklist was not named"

release_json="$test_root/release.json"
run_valid --offhost-evidence "$offhost" --restore-evidence "$restore" --checklist "$list" \
  --json-output "$release_json"
assert_status "$status" 0 "a complete candidate was not approved: $output"
assert_contains "$output" "RELEASE STATUS: PASS" "the approved release was not stated"
"$python_command" - "$release_json" <<'PY' || fail "the approved run's evidence is not usable"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
assert document["release_status"] == "pass", document["release_status"]
assert document["operational_status"] == "pass", document["operational_status"]
assert document["blockers"] == [], document["blockers"]
assert all(phase["status"] == "pass" for phase in document["phases"]), document["phases"]
PY

# The same complete evidence must NOT produce a release approval when a release-critical suite did
# not actually run. "Every engineering gate passed" cannot include one nobody executed, and the
# honest remedy is to rerun somewhere it can — so this is an engineering failure, not an F003/F004
# blocker.
skipped_release_json="$test_root/release-with-skip.json"
status=0
output=$(
  cd "$project" && RC_COMPOSE_LOG="$compose_log" RC_NPM_LOG="$npm_log" RC_SHELL_SUITE_SKIP=1 \
  FINSPACE_COMPOSE_CMD="sh $test_root/compose-stub.sh" FINSPACE_PYTHON="$python_command" \
  sh "$gate" --project-root "$project" --candidate "$candidate" \
    --expect-alembic-head "$head_revision" --expect-alembic-count "$head_count" \
    --offhost-evidence "$offhost" --restore-evidence "$restore" --checklist "$list" \
    --json-output "$skipped_release_json" 2>&1
) || status=$?
assert_status "$status" 1 "a release was approved with a shell suite that never ran"
assert_missing "$output" "RELEASE STATUS: PASS" "a release was approved with an unexecuted suite"
assert_contains "$output" "environment.test.sh" "the unexecuted suite was not named"
"$python_command" - "$skipped_release_json" <<'CHECK' || fail "the skip was misattributed"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
assert document["release_status"] == "fail", document["release_status"]
assert document["engineering_status"] == "fail", document["engineering_status"]
# Not an operational blocker: nothing about F003 or F004 is missing here.
assert document["operational_status"] == "pass", document["operational_status"]
failed = [phase["name"] for phase in document["phases"] if phase["status"] == "fail"]
assert failed == ["shell-tests"], failed
CHECK

# An open P0/P1 is an assertion that the candidate is defective, not that paperwork is missing.
sed 's/"open_p0_p1": 0/"open_p0_p1": 1/' "$list" >"$acceptance/checklist-p0.json"
run_valid --offhost-evidence "$offhost" --restore-evidence "$restore" \
  --checklist "$acceptance/checklist-p0.json"
assert_status "$status" 1 "an open P0/P1 defect did not fail the release"

# An outstanding operator procedure is missing paperwork, so it blocks rather than fails.
sed 's/"production_acceptance_verified": true/"production_acceptance_verified": false/' "$list" \
  >"$acceptance/checklist-pending.json"
run_valid --offhost-evidence "$offhost" --restore-evidence "$restore" \
  --checklist "$acceptance/checklist-pending.json"
assert_status "$status" 3 "an outstanding operator procedure was treated as approval"

# Evidence bound to a different commit must never authorise this one.
sed "s/$candidate/$wrong_candidate/" "$restore" >"$acceptance/f004-other.json"
run_valid --offhost-evidence "$offhost" --restore-evidence "$acceptance/f004-other.json" \
  --checklist "$list"
assert_status "$status" 1 "evidence for another candidate was accepted"

# A drill that ran on the development machine is a rehearsal, not acceptance.
sed 's/"isolated_test_mode": false/"isolated_test_mode": true/' "$restore" \
  >"$acceptance/f004-rehearsal.json"
run_valid --offhost-evidence "$offhost" --restore-evidence "$acceptance/f004-rehearsal.json" \
  --checklist "$list"
assert_status "$status" 1 "a rehearsal was accepted as a disaster-recovery drill"

# A second directory on the same disk is not a second failure domain.
sed 's/"separate_failure_domain": true/"separate_failure_domain": false/' "$offhost" \
  >"$acceptance/f003-same-disk.json"
run_valid --offhost-evidence "$acceptance/f003-same-disk.json" --restore-evidence "$restore" \
  --checklist "$list"
assert_status "$status" 1 "a copy without a separate failure domain was accepted"

printf '{ not json\n' >"$acceptance/f003-broken.json"
invalid_json="$test_root/invalid-evidence.json"
run_valid --offhost-evidence "$acceptance/f003-broken.json" --restore-evidence "$restore" \
  --checklist "$list" --json-output "$invalid_json"
assert_status "$status" 1 "malformed evidence was accepted"

# Bad paperwork is not a broken build, and the evidence document has to say which one it was.
"$python_command" - "$invalid_json" <<'CHECK' || fail "invalid evidence was blamed on engineering"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
assert document["release_status"] == "fail", document["release_status"]
assert document["engineering_status"] == "pass", document["engineering_status"]
assert document["operational_status"] == "fail", document["operational_status"]
CHECK

# A credential pasted into an acceptance document is refused on its shape alone.
"$python_command" - "$offhost" "$acceptance/f003-secret.json" <<'BUILD' || fail "fixture failed"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
document["postgres_password"] = "hunter2"
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(document, handle)
BUILD
run_valid --offhost-evidence "$acceptance/f003-secret.json" --restore-evidence "$restore" \
  --checklist "$list"
assert_status "$status" 1 "evidence carrying a credential-shaped field was accepted"

run_valid --offhost-evidence "$acceptance/absent.json" --restore-evidence "$restore" \
  --checklist "$list"
assert_status "$status" 1 "a named but missing evidence file was ignored"

printf 'release-candidate-gate test: PASS\n'
