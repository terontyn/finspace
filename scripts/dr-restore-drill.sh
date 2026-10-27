#!/bin/sh
# Disaster-recovery restore drill: orchestration and evidence, on the HOST.
#
# This is the F004 procedure layer. It does not implement a second PostgreSQL restore — restore.sh
# stays authoritative and is invoked through the existing tools container. What this adds is the
# part a runbook cannot enforce on its own: refusing to run against production, proving the
# selected backup set is the one it claims to be, deciding release/schema compatibility before
# anything is touched, and leaving an auditable artifact behind either way.
#
# Phases, in order:
#   preflight   nothing is created yet; prove the host is clean and the set is restorable
#   restore     restore the selected verified dump into the fresh, still-empty database
#   verify      after the application is up: health, schema, and a safe financial data probe
#
# Every phase appends to one evidence file and can only make it more specific, never less.
# A failure writes a FAILED verdict rather than leaving no trace.
set -eu

umask 077

program="dr-restore-drill"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"
}

usage() {
  cat >&2 <<'USAGE'
Usage:
  dr-restore-drill.sh candidates --backup-root DIR
  dr-restore-drill.sh preflight  --set-dir DIR --confirm-clean-environment [options]
  dr-restore-drill.sh restore    --set-dir DIR --confirm-clean-environment [options]
  dr-restore-drill.sh verify     --set-dir DIR --confirm-clean-environment [options]

Options:
  --project-root DIR      Finspace checkout on this host (default /opt/finspace)
  --evidence FILE         acceptance artifact (default <project-root>/data/acceptance/dr-restore-<id>.json)
  --source-probe FILE     safe source probe captured before the backup (verify only)
  --isolated-test-mode    permit pre-existing Finspace runtime state; NOT valid for F004 acceptance
USAGE
}

# ---------------------------------------------------------------------------------------------
# Evidence
#
# Facts are accumulated as one "key<TAB>value" per line and rendered as JSON at every write, so an
# interrupted drill still leaves a readable artifact. Values are typed by an explicit key list
# rather than by guessing, and everything not on the boolean or number list is emitted as a string.
# ---------------------------------------------------------------------------------------------

facts_file=""
evidence_file=""
drill_id=""

BOOLEAN_KEYS="environment.isolated_test_mode environment.clean_host_proven \
environment.wrapper_installed_before environment.legacy_server_override_present \
backup.local_verified backup.offhost_verified backup.n8n_archive_included"
NUMBER_KEYS="environment.preexisting_containers environment.preexisting_volumes \
restore.target_tables_before"

fact() {
  # A tab separates key from value, so a value may contain spaces but never a newline.
  printf '%s\t%s\n' "$1" "$(printf '%s' "$2" | tr -d '\n\t')" >>"$facts_file"
}

fact_value() {
  # Last write wins: a later phase refines what an earlier one recorded.
  awk -F'\t' -v key="$1" '$1 == key { value = $2 } END { print value }' "$facts_file"
}

json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

json_for() {
  key="$1"
  value="$(fact_value "$key")"
  if [ -z "$value" ]; then
    printf 'null'
    return
  fi
  case " $BOOLEAN_KEYS $NUMBER_KEYS " in
    *" $key "*) printf '%s' "$value" ;;
    *) printf '"%s"' "$(json_escape "$value")" ;;
  esac
}

emit_field() {
  printf '    "%s": %s%s\n' "$1" "$(json_for "$2")" "$3"
}

emit_probe() {
  # The probe files hold only counts and one timestamp; they are inlined so the artifact is one
  # self-contained document.
  path="$(fact_value "$1")"
  if [ -n "$path" ] && [ -s "$path" ]; then
    sed 's/^/    /' "$path"
  else
    printf '    null\n'
  fi
}

write_evidence() {
  verdict="$1"
  failure="$2"
  [ -n "$evidence_file" ] || return 0
  partial="${evidence_file}.partial"
  {
    printf '{\n'
    printf '  "version": 1,\n'
    printf '  "drill_id": "%s",\n' "$drill_id"
    printf '  "started_at": %s,\n' "$(json_for meta.started_at)"
    printf '  "updated_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "phase": %s,\n' "$(json_for meta.phase)"
    printf '  "completed_phases": %s,\n' "$(json_for meta.completed_phases)"
    printf '  "verdict": "%s",\n' "$verdict"
    if [ -n "$failure" ]; then
      printf '  "failure_reason": "%s",\n' "$(json_escape "$failure")"
    else
      printf '  "failure_reason": null,\n'
    fi

    printf '  "environment": {\n'
    emit_field project_root environment.project_root ','
    emit_field isolated_test_mode environment.isolated_test_mode ','
    emit_field clean_host_proven environment.clean_host_proven ','
    emit_field preexisting_finspace_containers environment.preexisting_containers ','
    emit_field preexisting_finspace_volumes environment.preexisting_volumes ','
    emit_field wrapper_installed_before environment.wrapper_installed_before ','
    emit_field legacy_server_override_present environment.legacy_server_override_present ''
    printf '  },\n'

    printf '  "backup": {\n'
    emit_field set_dir backup.set_dir ','
    emit_field set_id backup.set_id ','
    emit_field dump_filename backup.dump_filename ','
    emit_field dump_sha256 backup.dump_sha256 ','
    emit_field alembic_revision backup.alembic_revision ','
    emit_field finspace_commit backup.finspace_commit ','
    emit_field finspace_tag backup.finspace_tag ','
    emit_field local_verified backup.local_verified ','
    emit_field offhost_verified backup.offhost_verified ','
    emit_field n8n_archive_included backup.n8n_archive_included ''
    printf '  },\n'

    printf '  "target": {\n'
    emit_field commit target.commit ','
    emit_field tag target.tag ','
    emit_field alembic_head target.alembic_head ''
    printf '  },\n'

    printf '  "compatibility": {\n'
    emit_field case compatibility.case ','
    emit_field decision compatibility.decision ''
    printf '  },\n'

    printf '  "restore": {\n'
    emit_field result restore.result ','
    emit_field target_tables_before restore.target_tables_before ','
    emit_field restored_revision restore.restored_revision ','
    emit_field migration restore.migration ','
    emit_field revision_after_migration restore.revision_after ''
    printf '  },\n'

    printf '  "verification": {\n'
    emit_field topology verification.topology ','
    emit_field services verification.services ','
    emit_field backend_health verification.backend_health ','
    emit_field backend_ready verification.backend_ready ','
    emit_field frontend_login verification.frontend_login ','
    emit_field alembic_current verification.alembic_current ','
    emit_field data_probe_comparison verification.data_probe_comparison ','
    emit_field data_probe_mismatches verification.data_probe_mismatches ','
    printf '    "source_probe":\n'
    emit_probe verification.source_probe_file
    printf '    ,\n'
    printf '    "restored_probe":\n'
    emit_probe verification.restored_probe_file
    printf '\n  },\n'

    printf '  "n8n_restore": %s,\n' "$(json_for n8n.restore)"
    printf '  "operator_acceptance": {\n'
    emit_field login operator.login ','
    emit_field ui_data_review operator.ui_data_review ''
    printf '  }\n'
    printf '}\n'
  } >"$partial"
  mv "$partial" "$evidence_file"
  chmod 600 "$evidence_file" 2>/dev/null || true
}

fail() {
  log "drill_failed reason=$1"
  write_evidence "FAILED" "$1"
  [ -z "$evidence_file" ] || log "drill_evidence file=$evidence_file"
  exit 1
}

# ---------------------------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------------------------

[ "$#" -ge 1 ] || { usage; exit 2; }
command_name="$1"
shift

set_dir=""
backup_root=""
project_root="/opt/finspace"
requested_evidence=""
source_probe=""
confirmed="false"
isolated="false"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --set-dir) [ "$#" -ge 2 ] || { usage; exit 2; }; set_dir="$2"; shift 2 ;;
    --backup-root) [ "$#" -ge 2 ] || { usage; exit 2; }; backup_root="$2"; shift 2 ;;
    --project-root) [ "$#" -ge 2 ] || { usage; exit 2; }; project_root="$2"; shift 2 ;;
    --evidence) [ "$#" -ge 2 ] || { usage; exit 2; }; requested_evidence="$2"; shift 2 ;;
    --source-probe) [ "$#" -ge 2 ] || { usage; exit 2; }; source_probe="$2"; shift 2 ;;
    --confirm-clean-environment) confirmed="true"; shift ;;
    --isolated-test-mode) isolated="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "$program: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

compose="${FINSPACE_COMPOSE:-finspace-compose}"

# ---------------------------------------------------------------------------------------------
# candidates — the only command that looks at more than one set, and it changes nothing
# ---------------------------------------------------------------------------------------------

if [ "$command_name" = "candidates" ]; then
  [ -n "$backup_root" ] || { echo "$program: --backup-root is required" >&2; exit 2; }
  [ -d "$backup_root/sets" ] || { echo "$program: no sets directory under $backup_root" >&2; exit 1; }
  printf '%-24s %-14s %-26s %s\n' "SET_ID" "LOCAL_VERIFIED" "ALEMBIC_REVISION" "COMMIT"
  for candidate in "$backup_root"/sets/*; do
    [ -d "$candidate" ] || continue
    manifest="$candidate/backup-set.json"
    report="$candidate/backup-set-report.json"
    [ -s "$manifest" ] && [ -s "$report" ] || continue
    id="$(basename "$candidate")"
    verified="$(sed -n 's/.*"local_verified": *\([a-z]*\).*/\1/p' "$report" | head -n 1)"
    revision="$(sed -n 's/.*"alembic_revision": *"\([^"]*\)".*/\1/p' "$manifest" | head -n 1)"
    commit="$(sed -n 's/.*"finspace_commit": *"\([^"]*\)".*/\1/p' "$manifest" | head -n 1)"
    printf '%-24s %-14s %-26s %s\n' "$id" "${verified:-unknown}" "${revision:-unknown}" \
      "$(printf '%s' "${commit:-unknown}" | cut -c1-12)"
  done
  echo
  echo "Selection is never automatic: pass one of these to --set-dir explicitly." >&2
  exit 0
fi

case "$command_name" in
  preflight|restore|verify) ;;
  *) echo "$program: unknown command: $command_name" >&2; usage; exit 2 ;;
esac

# ---------------------------------------------------------------------------------------------
# Shared setup for the three drill phases
# ---------------------------------------------------------------------------------------------

if [ "$confirmed" != "true" ]; then
  echo "$program: refusing to run without --confirm-clean-environment." >&2
  echo "$program: this command restores a database; it must never be aimed at production." >&2
  exit 2
fi
[ -n "$set_dir" ] || { echo "$program: --set-dir is required" >&2; exit 2; }
[ -d "$set_dir" ] || { echo "$program: backup set directory does not exist: $set_dir" >&2; exit 1; }
case "$project_root" in
  /*) ;;
  *) echo "$program: --project-root must be an absolute path" >&2; exit 2 ;;
esac
[ -d "$project_root" ] || { echo "$program: project root does not exist: $project_root" >&2; exit 1; }
for marker in docker-compose.yml compose.production.yml backend/alembic/versions; do
  [ -e "$project_root/$marker" ] ||
    { echo "$program: project root marker is missing: $marker" >&2; exit 1; }
done

set_id_from_dir="$(basename "$set_dir")"
drill_id="$(date -u +%Y-%m-%dT%H%M%SZ)"
if [ -n "$requested_evidence" ]; then
  evidence_file="$requested_evidence"
else
  evidence_file="$project_root/data/acceptance/dr-restore-${drill_id}.json"
fi
evidence_dir="${evidence_file%/*}"
[ -d "$evidence_dir" ] || mkdir -p "$evidence_dir"

facts_file="$(mktemp)"
work_dir="$(mktemp -d)"
# Only what this script created is ever removed.
trap 'rm -f -- "$facts_file"; rm -rf -- "$work_dir"' EXIT HUP INT TERM

# The three phases share one artifact. Everything a later phase can re-derive, it re-derives;
# what it cannot — the state of the host before the drill created anything — is carried forward
# from the phase that actually observed it.
previous_field() {
  sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" "$evidence_file" | head -n 1
}
previous_raw() {
  sed -n "s/.*\"$1\": *\\([^,]*\\),*\$/\\1/p" "$evidence_file" | head -n 1
}
carry() {
  value="$2"
  [ -z "$value" ] || [ "$value" = "null" ] || fact "$1" "$value"
}
if [ -s "$evidence_file" ]; then
  previous_id="$(previous_field drill_id)"
  [ -z "$previous_id" ] || drill_id="$previous_id"
  carry meta.started_at "$(previous_field started_at)"
  carry n8n.restore "$(previous_field n8n_restore)"
  carry meta.completed_phases "$(previous_field completed_phases)"
  carry environment.clean_host_proven "$(previous_raw clean_host_proven)"
  carry environment.preexisting_containers "$(previous_raw preexisting_finspace_containers)"
  carry environment.preexisting_volumes "$(previous_raw preexisting_finspace_volumes)"
  carry environment.wrapper_installed_before "$(previous_raw wrapper_installed_before)"
  carry environment.legacy_server_override_present "$(previous_raw legacy_server_override_present)"
fi

require_previous_phase() {
  # A later phase may not silently stand in for an earlier one. The clean-host proof is only
  # meaningful before the drill created any state, so restore and verify inherit it instead of
  # pretending to re-observe it afterwards. Completed phases are tracked separately from the
  # verdict, so a phase that failed can be fixed and re-run without unpicking the chain.
  case " $(fact_value meta.completed_phases) " in
    *" $1 "*) return 0 ;;
  esac
  fail "phase_${1}_has_not_completed_for_this_drill"
}

complete_phase() {
  completed="$(fact_value meta.completed_phases)"
  case " $completed " in
    *" $1 "*) ;;
    *) fact meta.completed_phases "$(printf '%s %s' "$completed" "$1" | sed 's/^ *//')" ;;
  esac
}
[ -n "$(fact_value meta.started_at)" ] || fact meta.started_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
[ -n "$(fact_value n8n.restore)" ] || fact n8n.restore "not_tested"
fact meta.phase "$command_name"
fact environment.project_root "$project_root"
fact environment.isolated_test_mode "$isolated"
fact operator.login "pending"
fact operator.ui_data_review "pending"

# ---------------------------------------------------------------------------------------------
# Backup set validation
#
# The set is validated in the portable layout that backup-offhost.sh stages and that a manual
# transfer reproduces. Nothing is selected implicitly: exactly the directory the operator named.
# ---------------------------------------------------------------------------------------------

json_string_field() {
  sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" "$2" | head -n 1
}
json_bool_field() {
  sed -n "s/.*\"$1\": *\\([a-z]*\\).*/\\1/p" "$2" | head -n 1
}

validate_set() {
  set_manifest="$set_dir/backup-set.json"
  set_report="$set_dir/backup-set-report.json"
  dump_path="$set_dir/database.dump"
  dump_manifest="$set_dir/database.manifest.json"

  [ -s "$set_manifest" ] || fail "set_manifest_missing"
  [ -s "$set_report" ] || fail "set_report_missing"
  [ -s "$dump_path" ] || fail "dump_missing"
  [ -s "$dump_manifest" ] || fail "dump_manifest_missing"

  set_id="$(json_string_field set_id "$set_manifest")"
  case "$set_id" in
    ????-??-??T??????Z) ;;
    *) fail "set_id_unsafe" ;;
  esac
  case "$set_id" in
    *[!0-9TZ-]*) fail "set_id_unsafe" ;;
  esac
  [ "$set_id" = "$set_id_from_dir" ] || fail "set_id_does_not_match_directory"
  fact backup.set_dir "$set_dir"
  fact backup.set_id "$set_id"

  dump_filename="$(json_string_field filename "$set_manifest")"
  case "$dump_filename" in
    finspace_*.dump) ;;
    *) fail "dump_filename_unsafe" ;;
  esac
  case "$dump_filename" in
    */*|*..*) fail "dump_filename_unsafe" ;;
  esac
  [ "$dump_filename" = "finspace_${set_id}.dump" ] || fail "dump_filename_does_not_match_set"
  fact backup.dump_filename "$dump_filename"

  expected_sha="$(json_string_field sha256 "$set_manifest")"
  [ ${#expected_sha} -eq 64 ] || fail "set_manifest_sha_malformed"
  case "$expected_sha" in
    *[!0-9a-f]*) fail "set_manifest_sha_malformed" ;;
  esac
  manifest_sha="$(json_string_field sha256 "$dump_manifest")"
  # The two records of the same digest must agree before either is trusted.
  [ "$manifest_sha" = "$expected_sha" ] || fail "set_and_dump_manifest_disagree"

  actual_sha="$(sha256sum "$dump_path" | awk '{print $1}')"
  [ "$actual_sha" = "$expected_sha" ] || fail "dump_sha256_mismatch"
  fact backup.dump_sha256 "$actual_sha"

  backup_revision="$(json_string_field alembic_revision "$set_manifest")"
  [ -n "$backup_revision" ] || fail "backup_revision_missing"
  case "$backup_revision" in
    *[!A-Za-z0-9_-]*) fail "backup_revision_unsafe" ;;
  esac
  manifest_revision="$(json_string_field alembic_revision "$dump_manifest")"
  [ "$manifest_revision" = "$backup_revision" ] || fail "revision_disagreement_between_manifests"
  fact backup.alembic_revision "$backup_revision"

  backup_commit="$(json_string_field finspace_commit "$set_manifest")"
  [ -n "$backup_commit" ] || fail "backup_commit_missing"
  case "$backup_commit" in
    *[!0-9a-f]*) fail "backup_commit_unsafe" ;;
  esac
  [ ${#backup_commit} -ge 7 ] || fail "backup_commit_too_short"
  fact backup.finspace_commit "$backup_commit"

  backup_tag="$(json_string_field finspace_tag "$set_manifest")"
  case "$backup_tag" in
    *[!A-Za-z0-9._-]*) backup_tag="" ;;
  esac
  [ -z "$backup_tag" ] || fact backup.finspace_tag "$backup_tag"

  local_verified="$(json_bool_field local_verified "$set_report")"
  offhost_verified="$(json_bool_field offhost_verified "$set_report")"
  case "$local_verified" in
    true|false) ;;
    *) fail "set_report_malformed" ;;
  esac
  fact backup.local_verified "$local_verified"
  fact backup.offhost_verified "${offhost_verified:-false}"
  # A set that was never restored into a throwaway database has not been proven restorable, and a
  # disaster-recovery drill must not be the first place that is discovered.
  [ "$local_verified" = "true" ] || fail "backup_set_not_locally_verified"

  report_set_id="$(json_string_field set_id "$set_report")"
  [ "$report_set_id" = "$set_id" ] || fail "set_report_describes_another_set"

  if [ -s "$set_dir/n8n-data.tar.gz" ]; then
    fact backup.n8n_archive_included "true"
  else
    fact backup.n8n_archive_included "false"
  fi

  # Where the portable set carries the digests of everything, honour them.
  if [ -s "$set_dir/SHA256SUMS" ]; then
    ( cd "$set_dir" && sha256sum -c SHA256SUMS >/dev/null 2>&1 ) || fail "transferred_set_checksums_failed"
  fi

  log "drill_set_validated set_id=$set_id revision=$backup_revision verified=$local_verified"
}

# ---------------------------------------------------------------------------------------------
# Target release: what this checkout is, and how far its migrations go
# ---------------------------------------------------------------------------------------------

git_metadata() {
  # Same command-scoped trust the backup runner uses: the drill may run as root against a checkout
  # owned by the operator. Never a global Git configuration, never safe.directory=*.
  git -c "safe.directory=$project_root" -C "$project_root" "$@" 2>/dev/null || true
}

read_target() {
  target_commit="$(git_metadata rev-parse HEAD)"
  case "$target_commit" in
    '') fail "target_commit_unavailable" ;;
    *[!0-9a-f]*) fail "target_commit_unsafe" ;;
  esac
  fact target.commit "$target_commit"
  target_tag="$(git_metadata describe --exact-match --tags HEAD)"
  case "$target_tag" in
    *[!A-Za-z0-9._-]*) target_tag="" ;;
  esac
  [ -z "$target_tag" ] || fact target.tag "$target_tag"

  versions="$project_root/backend/alembic/versions"
  # The head is the revision no other revision descends from. Reading it from the checkout means
  # compatibility is decided before an image is built, let alone started.
  grep -h '^revision: ' "$versions"/*.py |
    sed -n 's/^revision: *[^=]*= *"\([^"]*\)".*/\1/p' | sort >"$work_dir/revisions"
  grep -h '^down_revision: ' "$versions"/*.py |
    sed -n 's/^down_revision: *[^=]*= *"\([^"]*\)".*/\1/p' | sort >"$work_dir/parents"
  comm -23 "$work_dir/revisions" "$work_dir/parents" >"$work_dir/heads"
  head_count="$(wc -l <"$work_dir/heads" | tr -d ' ')"
  [ "$head_count" -eq 1 ] || fail "target_has_${head_count}_alembic_heads"
  target_head="$(cat "$work_dir/heads")"
  [ -n "$target_head" ] || fail "target_alembic_head_unreadable"
  fact target.alembic_head "$target_head"
  log "drill_target commit=$(printf '%s' "$target_commit" | cut -c1-12) head=$target_head"
}

decide_compatibility() {
  backup_revision="$(fact_value backup.alembic_revision)"
  target_head="$(fact_value target.alembic_head)"
  if [ "$backup_revision" = "$target_head" ]; then
    fact compatibility.case "A"
    fact compatibility.decision "restore_and_start"
    log "drill_compatibility case=A revision=$backup_revision"
    return 0
  fi
  # The backup's revision must exist in this release for a forward migration to be possible. If it
  # does not, the backup is newer than this release understands.
  if grep -qx "$backup_revision" "$work_dir/revisions"; then
    fact compatibility.case "B"
    fact compatibility.decision "restore_then_forward_migrate"
    log "drill_compatibility case=B backup=$backup_revision target_head=$target_head"
    return 0
  fi
  fact compatibility.case "C"
  fact compatibility.decision "refuse"
  # Never downgrade, never start a release against a schema it does not support.
  fail "backup_revision_newer_than_target_release"
}

# ---------------------------------------------------------------------------------------------
# Clean-host proof
#
# The assertion is "no pre-existing Finspace runtime or state", not "this Docker host is empty".
# ---------------------------------------------------------------------------------------------

finspace_containers() {
  docker ps -a --filter "label=com.docker.compose.project=finspace" --format '{{.Names}}' 2>/dev/null |
    sed '/^$/d'
}

finspace_volumes() {
  docker volume ls --format '{{.Name}}' 2>/dev/null |
    grep -E '^finspace_(postgres_data|redis_data|n8n_data)$' || true
}

prove_clean_host() {
  command -v docker >/dev/null 2>&1 || fail "docker_unavailable"
  container_count="$(finspace_containers | wc -l | tr -d ' ')"
  volume_count="$(finspace_volumes | wc -l | tr -d ' ')"
  fact environment.preexisting_containers "$container_count"
  fact environment.preexisting_volumes "$volume_count"

  if command -v finspace-compose >/dev/null 2>&1; then
    fact environment.wrapper_installed_before "true"
  else
    fact environment.wrapper_installed_before "false"
  fi
  if [ -e /etc/finspace/compose.server.yml ]; then
    fact environment.legacy_server_override_present "true"
  else
    fact environment.legacy_server_override_present "false"
  fi

  if [ "$container_count" -eq 0 ] && [ "$volume_count" -eq 0 ]; then
    fact environment.clean_host_proven "true"
    log "drill_clean_host containers=0 volumes=0"
    return 0
  fi

  fact environment.clean_host_proven "false"
  if [ "$isolated" = "true" ]; then
    # Documented escape for automated tests and rehearsals on a busy host. It is recorded in the
    # artifact precisely so it can never be mistaken for a clean-environment acceptance.
    log "drill_isolated_test_mode containers=$container_count volumes=$volume_count"
    return 0
  fi
  fail "existing_finspace_environment_detected"
}

# ---------------------------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------------------------

compose_tools() {
  $compose --profile tools run --rm backup "$@"
}

phase_preflight() {
  log "drill_preflight_started"
  prove_clean_host
  validate_set
  read_target
  decide_compatibility
  fact restore.result "not_run"
  fact restore.migration "not_run"
  complete_phase preflight
  write_evidence "PREFLIGHT_PASSED" ""
  log "drill_preflight_passed evidence=$evidence_file"
}

phase_restore() {
  log "drill_restore_started"
  # By now the drill has created PostgreSQL and Redis, so the host is legitimately no longer bare.
  # The clean-host proof therefore comes from preflight, which ran before any of that existed, and
  # the production guard at this point is the one below: an empty target database.
  require_previous_phase preflight
  validate_set
  read_target
  decide_compatibility

  set_id="$(fact_value backup.set_id)"
  dump_filename="$(fact_value backup.dump_filename)"
  expected_sha="$(fact_value backup.dump_sha256)"

  # restore.sh accepts a dump only from inside /backups, which the tools container already mounts
  # from <project-root>/backups. Staging is a copy into that existing contract, not a new mount and
  # not a second restore path.
  staging_dir="$project_root/backups/database"
  [ -d "$staging_dir" ] || mkdir -p "$staging_dir"
  staged="$staging_dir/$dump_filename"
  if [ ! -e "$staged" ]; then
    cp "$set_dir/database.dump" "$staged.partial"
    mv "$staged.partial" "$staged"
  fi
  cp "$set_dir/database.manifest.json" "$staged.manifest.json.partial"
  mv "$staged.manifest.json.partial" "$staged.manifest.json"
  staged_sha="$(sha256sum "$staged" | awk '{print $1}')"
  [ "$staged_sha" = "$expected_sha" ] || fail "staged_dump_sha256_mismatch"
  log "drill_dump_staged file=$dump_filename"

  # The decisive production guard. A database that already holds tables is not a fresh drill
  # target, and this refuses rather than asking the operator to be careful.
  state="$(compose_tools sh /scripts/dr-data-probe.sh --schema-state 2>/dev/null || true)"
  tables="$(printf '%s\n' "$state" | sed -n 's/^tables=\([0-9][0-9]*\)$/\1/p' | head -n 1)"
  [ -n "$tables" ] || fail "target_database_state_unreadable"
  fact restore.target_tables_before "$tables"
  [ "$tables" -eq 0 ] || fail "target_database_is_not_empty"
  log "drill_target_database_empty tables=0"

  # restore.sh stays authoritative. POSTGRES_DB is resolved inside the container, where it is
  # already configured; the dump path arrives as a positional argument, never interpolated.
  if ! compose_tools sh -c \
    'RESTORE_CONFIRMATION="OVERWRITE $POSTGRES_DB" exec sh /scripts/restore.sh "$1" "$POSTGRES_DB" --overwrite-main' \
    dr-restore "/backups/database/$dump_filename" >"$work_dir/restore.log" 2>&1; then
    tail -n 5 "$work_dir/restore.log" >&2 || true
    fact restore.result "failed"
    fail "restore_failed"
  fi
  fact restore.result "succeeded"

  state="$(compose_tools sh /scripts/dr-data-probe.sh --schema-state 2>/dev/null || true)"
  restored_revision="$(printf '%s\n' "$state" | sed -n 's/^revision=\(.*\)$/\1/p' | head -n 1)"
  [ -n "$restored_revision" ] || fail "restored_revision_unreadable"
  fact restore.restored_revision "$restored_revision"
  backup_revision="$(fact_value backup.alembic_revision)"
  [ "$restored_revision" = "$backup_revision" ] || fail "restored_revision_does_not_match_backup"
  log "drill_restored revision=$restored_revision"

  if [ "$(fact_value compatibility.case)" = "B" ]; then
    log "drill_forward_migration_started"
    if ! $compose run --rm --no-deps backend alembic upgrade head >"$work_dir/migrate.log" 2>&1; then
      tail -n 5 "$work_dir/migrate.log" >&2 || true
      fact restore.migration "failed"
      fail "forward_migration_failed"
    fi
    fact restore.migration "applied"
    state="$(compose_tools sh /scripts/dr-data-probe.sh --schema-state 2>/dev/null || true)"
    after="$(printf '%s\n' "$state" | sed -n 's/^revision=\(.*\)$/\1/p' | head -n 1)"
    fact restore.revision_after "$after"
    [ "$after" = "$(fact_value target.alembic_head)" ] || fail "migration_did_not_reach_target_head"
    log "drill_forward_migration_finished revision=$after"
  else
    fact restore.migration "not_required"
    fact restore.revision_after "$restored_revision"
  fi

  complete_phase restore
  write_evidence "RESTORE_PASSED" ""
  log "drill_restore_passed evidence=$evidence_file"
}

COMPARED_KEYS="workspaces users workspace_members accounts_total accounts_active \
categories_total categories_active payees transactions_total transactions_active \
transaction_splits budget_periods budget_allocations goals recurring_rules import_batches \
month_closures google_sheet_bindings google_connections latest_transaction_occurred_at"

probe_value() {
  sed -n "s/^[[:space:]]*\"$2\": *\\(.*\\)\$/\\1/p" "$1" | head -n 1 | sed 's/,$//'
}

phase_verify() {
  log "drill_verify_started"
  require_previous_phase restore
  validate_set
  read_target

  if $compose config --format json 2>/dev/null |
    python3 "$project_root/backend/scripts/validate_compose_topology.py" production --stdin \
    >/dev/null 2>&1; then
    fact verification.topology "PASS"
  else
    fact verification.topology "FAIL"
  fi

  services=""
  for service in postgres redis backend frontend sync-worker categorization-prune; do
    running="$($compose ps --status running --services 2>/dev/null | grep -Fx "$service" || true)"
    if [ -n "$running" ]; then
      services="${services}${service}=running "
    else
      services="${services}${service}=down "
    fi
  done
  fact verification.services "$services"

  http_status() {
    curl -fsS -o /dev/null -w '%{http_code}' --max-time 15 "$1" 2>/dev/null || printf 'unreachable'
  }
  fact verification.backend_health "$(http_status http://127.0.0.1:8000/api/v1/health)"
  fact verification.backend_ready "$(http_status http://127.0.0.1:8000/api/v1/health/ready)"
  fact verification.frontend_login "$(http_status http://127.0.0.1:3000/login)"

  # Alembic logs to stderr, which is dropped; the revision is the one bare identifier left on
  # stdout, printed last and possibly followed by "(head)". Anchoring the whole line means a stray
  # log line cannot be mistaken for a revision — an unreadable answer then fails the drill rather
  # than passing it with the wrong value.
  current="$($compose run --rm --no-deps backend alembic current 2>/dev/null |
    sed -e 's/[[:space:]]*(head)[[:space:]]*$//' |
    sed -n 's/^\([0-9A-Za-z_][0-9A-Za-z_]*\)[[:space:]]*$/\1/p' | tail -n 1)"
  fact verification.alembic_current "${current:-unreadable}"

  restored_probe="$work_dir/restored-probe.json"
  if compose_tools sh /scripts/dr-data-probe.sh >"$restored_probe" 2>/dev/null &&
    [ -s "$restored_probe" ]; then
    kept="$evidence_dir/dr-restore-${drill_id}-restored-probe.json"
    cp "$restored_probe" "$kept.partial" && mv "$kept.partial" "$kept"
    chmod 600 "$kept" 2>/dev/null || true
    fact verification.restored_probe_file "$kept"
  else
    fail "restored_data_probe_failed"
  fi

  if [ -n "$source_probe" ]; then
    [ -s "$source_probe" ] || fail "source_probe_missing"
    fact verification.source_probe_file "$source_probe"
    mismatches=""
    for key in $COMPARED_KEYS; do
      source_value="$(probe_value "$source_probe" "$key")"
      restored_value="$(probe_value "$restored_probe" "$key")"
      if [ -z "$source_value" ] || [ -z "$restored_value" ]; then
        mismatches="${mismatches}${key}:unreadable "
      elif [ "$source_value" != "$restored_value" ]; then
        mismatches="${mismatches}${key} "
      fi
    done
    if [ -z "$mismatches" ]; then
      fact verification.data_probe_comparison "match"
    else
      fact verification.data_probe_comparison "mismatch"
      fact verification.data_probe_mismatches "$mismatches"
    fi
  else
    # Stating that no comparison was made is the honest outcome; a probe taken at a different
    # logical moment is not comparable and must not be presented as if it were.
    fact verification.data_probe_comparison "not_compared"
  fi

  verdict="VERIFY_PASSED"
  failure=""
  [ "$(fact_value verification.topology)" = "PASS" ] || { verdict="FAILED"; failure="topology_failed"; }
  case "$(fact_value verification.services)" in
    *=down*) verdict="FAILED"; failure="service_not_running" ;;
  esac
  [ "$(fact_value verification.backend_ready)" = "200" ] ||
    { verdict="FAILED"; failure="backend_not_ready"; }
  [ "$(fact_value verification.frontend_login)" = "200" ] ||
    { verdict="FAILED"; failure="frontend_login_unreachable"; }
  [ "$(fact_value verification.alembic_current)" = "$(fact_value target.alembic_head)" ] ||
    { verdict="FAILED"; failure="alembic_revision_is_not_target_head"; }
  [ "$(fact_value verification.data_probe_comparison)" != "mismatch" ] ||
    { verdict="FAILED"; failure="data_probe_mismatch"; }

  [ "$verdict" != "VERIFY_PASSED" ] || complete_phase verify
  write_evidence "$verdict" "$failure"
  log "drill_verify_finished verdict=$verdict evidence=$evidence_file"
  # The artifact still records the two operator steps as pending: an automated verdict is not
  # acceptance. A human still has to log in and look at the restored data.
  [ "$verdict" = "VERIFY_PASSED" ] || exit 1
  echo "Automated verification passed. F004 acceptance still requires an operator login and a" >&2
  echo "review of representative restored financial data in the UI." >&2
}

case "$command_name" in
  preflight) phase_preflight ;;
  restore) phase_restore ;;
  verify) phase_verify ;;
esac
