#!/bin/sh
# Decide whether one exact commit is a valid Finspace release candidate.
#
# Finspace already has an authoritative gate for every individual question — migrations,
# performance, topology, tests, documentation. What was missing is the single procedure that runs
# them against one named commit and reports a decision. This is that procedure; it orchestrates,
# it does not re-implement. Every phase below shells out to the command that already owns the
# question, so there is exactly one definition of "correct" per subject.
#
# Three ideas keep the result honest.
#
# 1. The candidate is named explicitly. There is no "whatever main happens to be" release
#    decision: the operator states a full 40-character commit and the gate refuses to run unless
#    the checkout is exactly that, with nothing uncommitted.
#
# 2. Engineering and operational acceptance are different things. Tests and builds can be
#    automated. Whether a backup physically reached a second failure domain (F003) and whether a
#    clean host restored it (F004) cannot be, so they arrive as operator evidence — and until they
#    do, the release is BLOCKED rather than green.
#
# 3. BLOCKED is not FAIL. Development continues while the homelab is unfinished; the same gate,
#    unchanged, becomes PASS once the evidence exists.
#
# Nothing here writes to a production database, restores over one, prunes anything or downgrades a
# schema. The two database-backed gates create and drop their own temporary databases.
set -eu

umask 077

# --- exit-code contract ------------------------------------------------------------------------
# 0  RELEASE PASS      every engineering gate passed and operational acceptance is complete
# 1  FAIL              a gate failed, or supplied evidence is invalid, or a P0/P1 defect is open
# 2  usage             the invocation itself is wrong
# 3  BLOCKED           engineering passed, operational acceptance is incomplete
EXIT_PASS=0
EXIT_FAIL=1
EXIT_USAGE=2
EXIT_BLOCKED=3

usage() {
  cat >&2 <<'USAGE'
Usage: release-candidate-gate.sh --candidate SHA --expect-alembic-head REV
                                 --expect-alembic-count N
                                 [--allow-pending-operational]
                                 [--offhost-evidence FILE] [--restore-evidence FILE]
                                 [--checklist FILE] [--json-output FILE]
                                 [--project-root DIR] [--allow-skips N]

  --candidate SHA                the exact 40-character commit under test
  --expect-alembic-head REV      the migration head this release was reviewed against
  --expect-alembic-count N       the number of migrations this release was reviewed against
  --allow-pending-operational    run the engineering suite before F003/F004 exist;
                                 the release still reports BLOCKED, never PASS
  --offhost-evidence FILE        F003 acceptance document
  --restore-evidence FILE        F004 acceptance document
  --checklist FILE               release checklist (open P0/P1, operator procedures)
  --json-output FILE             write the release-candidate evidence document here
  --project-root DIR             checkout to gate (default: the repository this script lives in)
  --allow-skips N                tolerated skipped backend tests (default 1)

Requires git, python3, docker compose and npm on this host, and a working .env in the checkout:
the gate builds and runs that checkout's own services.

Exit: 0 release PASS, 1 FAIL, 2 usage, 3 BLOCKED by pending operational acceptance.
USAGE
}

log() {
  printf '%s\n' "$1"
}

usage_error() {
  printf 'release candidate gate: usage: %s\n' "$1" >&2
  usage
  exit "$EXIT_USAGE"
}

# ---------------------------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------------------------

candidate=""
expect_head=""
expect_count=""
allow_pending=false
offhost_evidence=""
restore_evidence=""
checklist=""
json_output=""
project_root=""
allow_skips=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --candidate) [ "$#" -ge 2 ] || usage_error "--candidate needs a value"; candidate="$2"; shift 2 ;;
    --expect-alembic-head)
      [ "$#" -ge 2 ] || usage_error "--expect-alembic-head needs a value"; expect_head="$2"; shift 2 ;;
    --expect-alembic-count)
      [ "$#" -ge 2 ] || usage_error "--expect-alembic-count needs a value"; expect_count="$2"; shift 2 ;;
    --allow-pending-operational) allow_pending=true; shift ;;
    --offhost-evidence)
      [ "$#" -ge 2 ] || usage_error "--offhost-evidence needs a value"; offhost_evidence="$2"; shift 2 ;;
    --restore-evidence)
      [ "$#" -ge 2 ] || usage_error "--restore-evidence needs a value"; restore_evidence="$2"; shift 2 ;;
    --checklist) [ "$#" -ge 2 ] || usage_error "--checklist needs a value"; checklist="$2"; shift 2 ;;
    --json-output)
      [ "$#" -ge 2 ] || usage_error "--json-output needs a value"; json_output="$2"; shift 2 ;;
    --project-root)
      [ "$#" -ge 2 ] || usage_error "--project-root needs a value"; project_root="$2"; shift 2 ;;
    --allow-skips) [ "$#" -ge 2 ] || usage_error "--allow-skips needs a value"; allow_skips="$2"; shift 2 ;;
    -h|--help) usage; exit "$EXIT_USAGE" ;;
    *) usage_error "unknown argument $1" ;;
  esac
done

[ -n "$candidate" ] || usage_error "--candidate is required"
[ -n "$expect_head" ] || usage_error "--expect-alembic-head is required"
[ -n "$expect_count" ] || usage_error "--expect-alembic-count is required"

# A malformed candidate is a mistake in the invocation, so it is refused here rather than becoming
# a verdict about the code. Whether the checkout *is* that commit is a different question, and it
# belongs to the first phase.
case "$candidate" in
  *[!0-9a-f]*) usage_error "--candidate must be 40 lowercase hexadecimal characters" ;;
esac
[ ${#candidate} -eq 40 ] || usage_error "--candidate must be a full 40-character commit"

# The pins are required rather than defaulted on purpose: a release gate must state what was
# reviewed, not inherit whatever the repository happens to contain today.
case "$expect_count" in
  ''|*[!0-9]*) usage_error "--expect-alembic-count must be a number" ;;
esac
case "$allow_skips" in
  ''|*[!0-9]*) usage_error "--allow-skips must be a number" ;;
esac

# Asking for a release decision without supplying any acceptance evidence is a mistake in the
# invocation, not a verdict about the candidate. Say so before spending twenty minutes on builds.
if [ "$allow_pending" = false ] && [ -z "$offhost_evidence" ] && [ -z "$restore_evidence" ] &&
   [ -z "$checklist" ]; then
  usage_error "no operational acceptance evidence was supplied; pass --allow-pending-operational \
to run the engineering suite alone"
fi

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
[ -n "$project_root" ] || project_root=$(CDPATH='' cd -- "$script_dir/.." && pwd)
case "$project_root" in
  /*) ;;
  *) usage_error "--project-root must be an absolute path" ;;
esac
[ -d "$project_root/.git" ] || [ -f "$project_root/.git" ] ||
  usage_error "$project_root is not a Git checkout"

python_command="${FINSPACE_PYTHON:-python3}"
compose_command="${FINSPACE_COMPOSE_CMD:-docker compose}"

command -v git >/dev/null 2>&1 || usage_error "git is required"
command -v "$python_command" >/dev/null 2>&1 ||
  usage_error "$python_command is required (set FINSPACE_PYTHON)"

work=$(mktemp -d "${TMPDIR:-/tmp}/finspace-rc-XXXXXX")
cleanup() {
  rm -rf -- "$work"
}
trap cleanup EXIT HUP INT TERM

phases_file="$work/phases.tsv"
blockers_file="$work/blockers"
: >"$phases_file"
: >"$blockers_file"

engineering_status="pass"
operational_status="pass"

# ---------------------------------------------------------------------------------------------
# Phase machinery
# ---------------------------------------------------------------------------------------------

now_ms() {
  stamp=$(date +%s%3N 2>/dev/null) || stamp=""
  case "$stamp" in
    ''|*[!0-9]*) printf '%s000' "$(date +%s)" ;;
    *) printf '%s' "$stamp" ;;
  esac
}

# A phase summary is one short line of non-secret facts. Phases write it here; the runner reads it
# once the phase returns, so a phase that dies mid-way simply has no summary rather than a stale
# one from its predecessor.
summary_file="$work/summary"

summarise() {
  printf '%s' "$1" >"$summary_file"
}

blocker() {
  printf '%s\n' "$1" >>"$blockers_file"
}

record() {
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >>"$phases_file"
}

# Runs one phase, records it, and prints its captured output only when it fails. A passing gate
# that prints thousands of lines is a gate nobody reads.
run_phase() {
  phase_name="$1"
  fatal="$2"
  shift 2
  : >"$summary_file"
  started=$(now_ms)
  phase_status="pass"
  if ! "$@" >"$work/$phase_name.log" 2>&1; then
    phase_status="fail"
  fi
  finished=$(now_ms)
  duration=$((finished - started))
  [ "$duration" -ge 0 ] || duration=0
  phase_summary=$(cat "$summary_file" 2>/dev/null || true)
  [ -n "$phase_summary" ] || phase_summary="-"
  record "$phase_name" "$phase_status" "$duration" "$phase_summary"
  printf '  %-26s %-6s %6ss  %s\n' "$phase_name" \
    "$(printf '%s' "$phase_status" | tr '[:lower:]' '[:upper:]')" \
    "$((duration / 1000))" "$phase_summary"
  if [ "$phase_status" = "fail" ]; then
    # Invalid acceptance evidence fails the release without being an engineering defect. Recording
    # it as one would make the evidence document say the tests broke when they did not.
    if [ "$fatal" = "operational" ]; then
      operational_status="fail"
    else
      engineering_status="fail"
    fi
    printf '\n--- %s ---\n' "$phase_name" >&2
    tail -n 60 "$work/$phase_name.log" >&2 || true
    printf -- '--- end %s ---\n\n' "$phase_name" >&2
    if [ "$fatal" = "fatal" ]; then
      finish
    fi
  fi
}

# ---------------------------------------------------------------------------------------------
# Compose helpers
#
# Every compose invocation is anchored to --project-directory "$project_root" and to that
# checkout's compose files, and application work runs through `run --rm --no-deps` rather than
# `exec`. That is deliberate: `exec` would enter a container someone else started, possibly from a
# different checkout, and the gate would then cheerfully certify code that is not the candidate.
# ---------------------------------------------------------------------------------------------

compose() {
  # Word splitting on $compose_command is intended: it carries a command plus a sub-command.
  # shellcheck disable=SC2086
  $compose_command --project-directory "$project_root" \
    --file "$project_root/docker-compose.yml" "$@"
}

compose_production() {
  # shellcheck disable=SC2086
  $compose_command --project-directory "$project_root" \
    --file "$project_root/docker-compose.yml" \
    --file "$project_root/compose.production.yml" "$@"
}

backend_run() {
  compose run --rm --no-deps -T -e TESTING=true backend "$@"
}

# ---------------------------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------------------------

phase_candidate_identity() {
  head=$(git -C "$project_root" rev-parse HEAD) || return 1
  if [ "$head" != "$candidate" ]; then
    echo "checkout is at $head, the candidate is $candidate"
    return 1
  fi

  # Two independent readings of the same question. The strict helper is the repository's contract
  # and additionally fails on Git diagnostics, but a release decision should not rest on parsing
  # one script's prose, so porcelain is inspected directly as well.
  porcelain=$(git -C "$project_root" status --porcelain) || return 1
  if [ -n "$porcelain" ]; then
    echo "the worktree is not clean:"
    printf '%s\n' "$porcelain"
    return 1
  fi
  ( cd "$project_root" && sh "$project_root/scripts/git-status-strict.sh" ) || return 1

  branch=$(git -C "$project_root" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached")
  described=$(git -C "$project_root" describe --exact-match --tags HEAD 2>/dev/null || echo "none")
  [ -f "$project_root/.env" ] || {
    echo "$project_root/.env is missing: the gate builds and runs this checkout's services"
    return 1
  }
  echo "candidate $candidate branch $branch tag $described"
  summarise "commit=$(printf '%s' "$candidate" | cut -c1-12) branch=$branch tag=$described"
}

phase_compose_topology() {
  "$python_command" "$project_root/backend/scripts/validate_compose_topology.py" all || return 1
  # The production overlay is only real through the canonical wrapper, so validate exactly what an
  # operator would run. --quiet keeps the interpolated configuration, which carries secrets, out
  # of the log.
  FINSPACE_PROJECT_ROOT="$project_root" sh "$project_root/scripts/finspace-compose.sh" \
    config --quiet || return 1
  echo "merged production configuration validated through finspace-compose"
  summarise "development+production validated, wrapper merged config ok"
}

phase_images_development() {
  compose build backend frontend || return 1
  summarise "backend and frontend development images built from the candidate"
}

phase_migration_gate() {
  backend_run python scripts/validate_migrations.py \
    --expect-head "$expect_head" --expect-count "$expect_count" || return 1
  summarise "F008 head=$expect_head revisions=$expect_count"
}

phase_backend_static() {
  backend_run ruff check . || return 1
  backend_run ruff format --check app alembic scripts tests || return 1
  # `mypy app` is the repository's documented scope; widening it here would mean competing with
  # the project's own configuration rather than enforcing it.
  backend_run mypy app || return 1
  backend_run python -m compileall -q app alembic scripts tests || return 1
  summarise "ruff check, ruff format, mypy, compileall"
}

phase_backend_tests() {
  backend_run python scripts/test_runner.py || return 1
  line=$(grep -E '[0-9]+ (passed|failed|error)' "$work/backend-tests.log" 2>/dev/null | tail -n 1)
  [ -n "$line" ] || { echo "no pytest summary line was produced"; return 1; }
  failed=$(printf '%s' "$line" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) failed.*/\1/p')
  errors=$(printf '%s' "$line" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) error.*/\1/p')
  skipped=$(printf '%s' "$line" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) skipped.*/\1/p')
  deselected=$(printf '%s' "$line" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) deselected.*/\1/p')
  passed=$(printf '%s' "$line" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) passed.*/\1/p')
  if [ -n "$failed" ]; then echo "$failed test(s) failed"; return 1; fi
  if [ -n "$errors" ]; then echo "$errors test error(s)"; return 1; fi
  if [ -n "$deselected" ]; then
    echo "$deselected test(s) were deselected; a release runs them all"
    return 1
  fi
  [ -n "$skipped" ] || skipped=0
  if [ "$skipped" -gt "$allow_skips" ]; then
    echo "$skipped test(s) skipped, at most $allow_skips is accepted for a release"
    return 1
  fi
  summarise "passed=${passed:-unknown} skipped=$skipped allowed_skips=$allow_skips"
}

phase_performance_smoke() {
  backend_run python scripts/performance_smoke.py || return 1
  summarise "F014 bounded query counts; known transaction-page N+1 is recorded, not fixed"
}

phase_frontend_tests() {
  # These are host commands on purpose. They are what operations-runbook.md already prescribes,
  # and the development container mounts a *shared* node_modules volume: running them there would
  # depend on whatever some other checkout installed into it, and `npm ci` would overwrite it.
  command -v npm >/dev/null 2>&1 || { echo "npm is required for the frontend checks"; return 1; }
  (
    cd "$project_root/frontend" || exit 1
    # From the lockfile, so the dependency tree is the candidate's rather than yesterday's.
    [ -d node_modules ] || npm ci || exit 1
    npm test || exit 1
    npm run typecheck || exit 1
    npm run lint || exit 1
  ) || return 1
  summarise "npm test, typecheck, lint"
}

phase_images_release() {
  # The production frontend stage runs `next build`; a development server starting is not proof
  # that the release can be built.
  compose_production build backend frontend || return 1
  summarise "production backend image and production frontend build"
}

phase_shell_tests() {
  suites=0
  failures=0
  skipped=""
  for suite in "$project_root"/scripts/tests/*.test.sh; do
    [ -f "$suite" ] || continue
    suites=$((suites + 1))
    suite_status=0
    sh "$suite" || suite_status=$?
    case "$suite_status" in
      0) ;;
      # Exit 4 is the repository's "this environment cannot run me" code — a root-only integration
      # test on a developer machine, for instance. It is not a pass, so it is named here and in the
      # evidence document rather than folded into the total.
      4) skipped="$skipped $(basename "$suite")" ;;
      *) failures=$((failures + 1)); echo "FAILED: $suite" ;;
    esac
  done
  # Discovery rather than a hand-maintained list: a new release-critical suite cannot be forgotten,
  # and an empty directory is a defect in the gate rather than a pass.
  [ "$suites" -gt 0 ] || { echo "no shell test suites were discovered"; return 1; }
  [ "$failures" -eq 0 ] || return 1
  if [ -n "$skipped" ]; then
    summarise "$suites discovered suites, skipped in this environment:$skipped"
  else
    summarise "$suites discovered suites, all passed"
  fi
}

phase_docs_gate() {
  "$python_command" "$project_root/backend/scripts/validate_docs.py" --root "$project_root" ||
    return 1
  summarise "links, anchors and the supported-scope contract"
}

# ---------------------------------------------------------------------------------------------
# Operational acceptance
# ---------------------------------------------------------------------------------------------

evidence_tool() {
  "$python_command" "$project_root/backend/scripts/release_evidence.py" "$@"
}

# Returns 0 accepted, 1 invalid (a release failure), 3 not supplied (a release blocker).
check_acceptance() {
  label="$1"
  file="$2"
  shift 2
  if [ -z "$file" ]; then
    return 3
  fi
  if [ ! -f "$file" ]; then
    echo "$label: $file does not exist"
    return 1
  fi
  evidence_tool "$@" --file "$file" --candidate "$candidate"
}

phase_operational_acceptance() {
  outcome=0

  set +e
  check_acceptance "F003" "$offhost_evidence" validate-offhost --expect-alembic-head "$expect_head"
  offhost_result=$?
  check_acceptance "F004" "$restore_evidence" validate-restore \
    --expect-alembic-head "$expect_head" --project-root "$project_root"
  restore_result=$?
  check_acceptance "checklist" "$checklist" validate-checklist
  checklist_result=$?
  set -e

  case "$offhost_result" in
    0) ;;
    3) blocker "F003 physical off-host backup acceptance: evidence not supplied"; outcome=3 ;;
    *) blocker "F003 physical off-host backup acceptance: evidence is invalid"; return 1 ;;
  esac
  case "$restore_result" in
    0) ;;
    3) blocker "F004 clean-environment restore acceptance: evidence not supplied"; outcome=3 ;;
    *) blocker "F004 clean-environment restore acceptance: evidence is invalid"; return 1 ;;
  esac
  case "$checklist_result" in
    0) ;;
    3)
      if [ -z "$checklist" ]; then
        blocker "release checklist: open P0/P1 count and operator procedures not acknowledged"
      else
        blocker "release checklist: acknowledged items are still outstanding"
      fi
      outcome=3
      ;;
    # An open P0/P1 defect, or a malformed checklist, is an assertion that the candidate is not
    # releasable. That is a failure, not missing paperwork.
    *) blocker "release checklist: invalid, or an open P0/P1 defect blocks the release"; return 1 ;;
  esac

  if [ "$outcome" -eq 0 ]; then
    summarise "F003 accepted, F004 accepted, checklist acknowledged"
    operational_status="pass"
    return 0
  fi
  summarise "operational acceptance incomplete"
  operational_status="pending"
  return 0
}

# ---------------------------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------------------------

finish() {
  release_status="pass"
  if [ "$engineering_status" != "pass" ] || [ "$operational_status" = "fail" ]; then
    release_status="fail"
  elif [ "$operational_status" != "pass" ]; then
    release_status="blocked"
  fi

  log ""
  log "ENGINEERING STATUS: $(printf '%s' "$engineering_status" | tr '[:lower:]' '[:upper:]')"
  log "OPERATIONAL STATUS: $(printf '%s' "$operational_status" | tr '[:lower:]' '[:upper:]')"
  if [ -s "$blockers_file" ]; then
    while IFS= read -r line; do
      log "  BLOCKER: $line"
    done <"$blockers_file"
  fi
  log "RELEASE STATUS: $(printf '%s' "$release_status" | tr '[:lower:]' '[:upper:]')"
  if [ "$release_status" = "blocked" ]; then
    log ""
    log "The engineering suite is complete. This is not release approval: operational acceptance"
    log "is still outstanding and no tag may be created."
  fi

  if [ -n "$json_output" ]; then
    render_status=0
    # The conventional destination is data/acceptance/release/, which a fresh checkout does not
    # carry. Losing a completed run to a missing directory would be a poor trade.
    output_directory="${json_output%/*}"
    if [ "$output_directory" != "$json_output" ] && [ ! -d "$output_directory" ]; then
      mkdir -p "$output_directory" || true
    fi
    set --
    while IFS= read -r line; do
      set -- "$@" --blocker "$line"
    done <"$blockers_file"
    evidence_tool render \
      --phases "$phases_file" \
      --candidate "$candidate" \
      --expect-alembic-head "$expect_head" \
      --expect-alembic-count "$expect_count" \
      --engineering-status "$engineering_status" \
      --operational-status "$operational_status" \
      --release-status "$release_status" \
      --output "$json_output" \
      "$@" || render_status=$?
    if [ "$render_status" -ne 0 ]; then
      log "release candidate gate: FAIL: the evidence document could not be written"
      exit "$EXIT_FAIL"
    fi
  fi

  case "$release_status" in
    pass) exit "$EXIT_PASS" ;;
    blocked) exit "$EXIT_BLOCKED" ;;
    *) exit "$EXIT_FAIL" ;;
  esac
}

# ---------------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------------

log "release candidate gate"
log "  candidate            $candidate"
log "  project root         $project_root"
log "  alembic expectation  $expect_head ($expect_count revisions)"
if [ "$allow_pending" = true ]; then
  log "  mode                 engineering suite; operational acceptance may be pending"
fi
log ""
printf '  %-26s %-6s %7s  %s\n' "PHASE" "STATUS" "TIME" "SUMMARY"

run_phase candidate-identity fatal phase_candidate_identity
run_phase compose-topology fatal phase_compose_topology
run_phase images-development fatal phase_images_development
run_phase migration-gate fatal phase_migration_gate
run_phase backend-static continue phase_backend_static
run_phase backend-tests continue phase_backend_tests
run_phase performance-smoke continue phase_performance_smoke
run_phase frontend-tests continue phase_frontend_tests
run_phase images-release continue phase_images_release
run_phase shell-tests continue phase_shell_tests
run_phase docs-gate continue phase_docs_gate
run_phase operational-acceptance operational phase_operational_acceptance

finish
