#!/bin/sh
# One complete unattended backup run, on the HOST.
#
# Orchestrates the proven Stage A pieces and nothing else: it creates no artifact formats of its
# own, moves no dump, and reimplements no retention algorithm. What it adds is ordering, locking
# and a deterministic handoff, so an unattended run can never guess which dump it produced or leave
# retention running after a failed off-host copy.
#
# Sequence, off-host enabled:
#   flock -> create dump -> build set (verifies exactly once) -> off-host copy
#         -> database retention -> set retention
# Sequence, off-host explicitly disabled:
#   flock -> create dump -> build set -> warn -> database retention -> set retention
#
# Retention never runs after a failed off-host copy: previous usable restore points must survive a
# bad new run. Remote sets are append-only in Stage B; nothing here deletes anything remote.
set -eu

umask 077

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"
}

fail() {
  log "backup_run_failed reason=$1"
  exit 1
}

lock_file="${FINSPACE_BACKUP_LOCK_FILE:-/run/finspace-backup.lock}"

# Run under flock so a manual invocation and a timer invocation share one lock. A dedicated
# conflict exit code keeps "the lock is held" distinguishable from "the run itself failed", which a
# bare flock exit status cannot express. The guard variable prevents an infinite re-entry loop.
if [ "${FINSPACE_BACKUP_LOCKED:-}" != "1" ]; then
  if ! command -v flock >/dev/null 2>&1; then
    log "backup_run_failed reason=flock_unavailable"
    exit 1
  fi
  FINSPACE_BACKUP_LOCKED=1
  export FINSPACE_BACKUP_LOCKED
  status=0
  flock --nonblock --conflict-exit-code 4 "$lock_file" "$0" "$@" || status=$?
  if [ "$status" -eq 4 ]; then
    log "backup_run_locked lock=busy"
    exit 1
  fi
  exit "$status"
fi

log "backup_run_started"

project_root="${FINSPACE_PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
backup_root="${FINSPACE_BACKUP_ROOT:-${project_root}/backups}"
compose="${FINSPACE_COMPOSE:-docker compose}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
offhost_enabled="${FINSPACE_BACKUP_OFFHOST_ENABLED:-true}"

case "$offhost_enabled" in
  true|false) ;;
  *) fail "invalid_offhost_flag" ;;
esac
[ -d "$backup_root" ] || fail "backup_root_missing"

# Release metadata is read here rather than trusted from the environment: a scheduled run has no
# operator to export it, and an inherited value could describe a different checkout entirely.
commit="$(git -C "$project_root" rev-parse HEAD 2>/dev/null || true)"
case "$commit" in
  '') fail "commit_unavailable" ;;
  *[!0-9a-f]*) fail "commit_unsafe" ;;
esac
tag="$(git -C "$project_root" describe --exact-match --tags HEAD 2>/dev/null || true)"
case "$tag" in
  *[!A-Za-z0-9._-]*) tag="" ;;
esac

result_file="${backup_root}/database/.backup-run-result"
rm -f "$result_file"

# --- create ---------------------------------------------------------------------------------
if ! $compose --profile tools run --rm \
  -e BACKUP_RESULT_FILE=/backups/database/.backup-run-result \
  backup sh /scripts/backup.sh >/dev/null; then
  fail "database_backup_failed"
fi
[ -s "$result_file" ] || fail "dump_handoff_missing"
dump_name="$(head -n 1 "$result_file")"
case "$dump_name" in
  finspace_*.dump) ;;
  *) fail "dump_handoff_unsafe" ;;
esac
case "$dump_name" in
  */*|*..*) fail "dump_handoff_unsafe" ;;
esac
set_id="$(printf '%s' "$dump_name" | sed -n 's/^finspace_\(.*\)\.dump$/\1/p')"
case "$set_id" in
  ????-??-??T??????Z) ;;
  *) fail "set_id_unsafe" ;;
esac
log "backup_run_created dump=$dump_name"

# --- verify and inventory -------------------------------------------------------------------
# backup-set.sh runs the full restore verification exactly once: pg_restore list, throwaway
# database restore, Alembic revision, required tables and columns. Nothing verifies twice.
if ! $compose --profile tools run --rm \
  -e FINSPACE_COMMIT="$commit" \
  -e FINSPACE_TAG="$tag" \
  backup sh /scripts/backup-set.sh "/backups/database/${dump_name}" >/dev/null; then
  fail "backup_set_failed"
fi
log "backup_run_local_verified set_id=$set_id"

# --- off-host -------------------------------------------------------------------------------
if [ "$offhost_enabled" = "true" ]; then
  if ! FINSPACE_BACKUP_ROOT="$backup_root" FINSPACE_COMPOSE="$compose" \
    sh "$script_dir/backup-offhost.sh" "$set_id"; then
    # Deliberately no retention here: a failed new off-host copy must never cost us the previous
    # restore points that are still safely stored.
    fail "offhost_copy_failed"
  fi
  log "backup_run_offhost_verified set_id=$set_id"
else
  # Explicit, temporary and loud. Local-only backups do not satisfy the v1.0 DR contract, and
  # backup_status will keep reporting the run as unverified until an off-host copy is confirmed.
  log "backup_run_offhost_skipped offhost_disabled=true"
  log "backup_run_degraded offhost_disabled=true local_only_backup_is_not_v1_0_dr"
fi

# --- retention ------------------------------------------------------------------------------
# The canonical PostgreSQL policy stays authoritative; this only decides when it runs.
if ! $compose --profile tools run --rm backup sh /scripts/backup-cleanup.sh >/dev/null; then
  fail "database_retention_failed"
fi
if ! FINSPACE_BACKUP_ROOT="$backup_root" sh "$script_dir/backup-set-cleanup.sh"; then
  fail "set_retention_failed"
fi
log "backup_run_retention_finished"

log "backup_run_finished set_id=$set_id offhost=$offhost_enabled"
