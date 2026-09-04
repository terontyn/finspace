#!/bin/sh
# What Finspace is storing on THIS host, and who owns reclaiming it. Read-only.
#
# The backend container cannot answer the whole question: the host keeps backups 0700 root-owned
# and the application runs unprivileged, so anything it reported about them would be wrong. This
# script fills that gap from the host and then calls the two tools that already exist rather than
# reimplementing either of them:
#
#   backend/scripts/data_lifecycle_report.py   PostgreSQL sizes and lifecycle classification
#   backend/scripts/import_staging_reclaim.py  staged-import classification (F010), inspect only
#
# It deletes nothing, moves nothing and reads no file's contents — only directory metadata and the
# small JSON records the backup pipeline already writes. Machine-readable output is deliberately
# not produced here: the stable JSON contract belongs to the Python report (`--json`).
set -eu

program="data-lifecycle-report"

usage() {
  cat >&2 <<'USAGE'
Usage: data-lifecycle-report.sh [options]

  --project-root DIR   Finspace checkout (default /opt/finspace)
  --no-database        skip the PostgreSQL report (filesystem only; no container is started)
  --no-imports         skip the F010 staged-import inspection
USAGE
}

project_root="/opt/finspace"
with_database="true"
with_imports="true"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --project-root) [ "$#" -ge 2 ] || { usage; exit 2; }; project_root="$2"; shift 2 ;;
    --no-database) with_database="false"; shift ;;
    --no-imports) with_imports="false"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "$program: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$project_root" in
  /*) ;;
  *) echo "$program: --project-root must be an absolute path" >&2; exit 2 ;;
esac
[ -d "$project_root" ] || { echo "$program: project root does not exist: $project_root" >&2; exit 1; }

compose="${FINSPACE_COMPOSE:-finspace-compose}"
failures=0

heading() {
  printf '\n== %s ==\n' "$1"
}

# Directory metadata only: how many files and how many bytes. Never a filename, never a content
# read, never a symlink followed, never deeper than the depth this layout actually has.
directory_usage() {
  label="$1"
  path="$2"
  depth="$3"
  owner="$4"

  if [ -L "$path" ]; then
    printf '%-28s %-12s %s\n' "$label" "SYMLINK" "refused: managed paths are not followed through links"
    failures=$((failures + 1))
    return 0
  fi
  if [ ! -e "$path" ]; then
    printf '%-28s %-12s %s\n' "$label" "absent" "$owner"
    return 0
  fi
  if [ ! -d "$path" ]; then
    printf '%-28s %-12s %s\n' "$label" "NOT A DIR" "$owner"
    failures=$((failures + 1))
    return 0
  fi
  if [ ! -r "$path" ] || [ ! -x "$path" ]; then
    # Explicitly unreadable beats a confident zero an operator might act on.
    printf '%-28s %-12s %s\n' "$label" "UNREADABLE" "not readable by this user; re-run with sudo"
    failures=$((failures + 1))
    return 0
  fi

  # find reports an unreadable subdirectory on stderr and carries on. Discarding that is exactly
  # how a partial traversal gets published as a total, so it is captured rather than silenced.
  # find exits non-zero on an unreadable subtree, which is exactly the case being detected here;
  # without the guard `set -e` would kill the report instead of letting it report the problem.
  scan_errors=$(find "$path" -maxdepth "$depth" 2>&1 >/dev/null || true)
  # A directory sitting at the maximum depth may hold files this traversal never reached.
  deeper=$(find "$path" -mindepth "$depth" -type d 2>/dev/null | head -n 1 || true)

  sizes=$(find "$path" -maxdepth "$depth" -type f -printf '%s\n' 2>/dev/null || true)
  count=$(printf '%s\n' "$sizes" | grep -c . || true)
  bytes=$(printf '%s\n' "$sizes" | awk '{ total += $1 } END { printf "%d", total + 0 }')

  if [ -n "$scan_errors" ] || [ -n "$deeper" ]; then
    if [ -n "$scan_errors" ]; then
      reason="part of the tree could not be read"
    else
      reason="content below depth $depth was not traversed"
    fi
    # A lower bound, marked as one: an incomplete total must never read as the answer.
    printf '%-28s %8s files %12s bytes  PARTIAL: %s\n' "$label" "$count" ">=$bytes" "$reason"
    failures=$((failures + 1))
    return 0
  fi

  printf '%-28s %8s files %12s bytes  %s\n' "$label" "$count" "$bytes" "$owner"
}

# --- host ---------------------------------------------------------------------------------------
heading "HOST"
printf 'project root                 %s\n' "$project_root"
printf 'generated at                 %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if command -v df >/dev/null 2>&1; then
  printf '\n'
  df -h "$project_root" 2>/dev/null || printf 'disk usage unavailable\n'
fi

# --- managed directories --------------------------------------------------------------------------
heading "MANAGED DIRECTORIES"
directory_usage "backups/database" "$project_root/backups/database" 1 \
  "backup-cleanup.sh: 7 daily + 4 weekly"
directory_usage "backups/sets" "$project_root/backups/sets" 2 \
  "backup-set-cleanup.sh: a set lives as long as its dump"
directory_usage "backups/acceptance-reports" "$project_root/backups/acceptance-reports" 1 \
  "operator evidence; never auto-reclaimed"
directory_usage "data/imports" "$project_root/data/imports" 1 \
  "F010 staged-import reclamation"
directory_usage "data/acceptance" "$project_root/data/acceptance" 1 \
  "operator evidence; never auto-reclaimed"

# --- newest backup, as already recorded -----------------------------------------------------------
# Reported, not judged. Whether a backup is usable is decided by verify-backup.sh and the backup
# status endpoint; a file existing proves nothing and this must not imply otherwise.
heading "NEWEST BACKUP SET (recorded facts only, not a verification)"
sets_root="$project_root/backups/sets"
if [ ! -d "$sets_root" ] || [ ! -r "$sets_root" ]; then
  printf 'no readable backup set directory on this host\n'
else
  newest=$(find "$sets_root" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -n 1)
  if [ -z "$newest" ]; then
    printf 'no backup sets yet\n'
  else
    report="$newest/backup-set-report.json"
    manifest="$newest/backup-set.json"
    printf 'set id                       %s\n' "$(basename "$newest")"
    if [ -r "$manifest" ]; then
      printf 'alembic revision             %s\n' \
        "$(sed -n 's/.*"alembic_revision": *"\([^"]*\)".*/\1/p' "$manifest" | head -n 1)"
    else
      printf 'alembic revision             unreadable\n'
    fi
    if [ -r "$report" ]; then
      printf 'local_verified               %s\n' \
        "$(sed -n 's/.*"local_verified": *\([a-z]*\).*/\1/p' "$report" | head -n 1)"
      printf 'offhost_verified             %s\n' \
        "$(sed -n 's/.*"offhost_verified": *\([a-z]*\).*/\1/p' "$report" | head -n 1)"
    else
      printf 'verification status          unreadable; re-run with sudo\n'
      failures=$((failures + 1))
    fi
    printf 'authority                    verify-backup.sh and /api/v1/automation/backup/status\n'
  fi
fi

# --- PostgreSQL -----------------------------------------------------------------------------------
if [ "$with_database" = "true" ]; then
  heading "POSTGRESQL"
  if ! $compose run --rm --no-deps backend python scripts/data_lifecycle_report.py; then
    printf '%s: the database report failed\n' "$program" >&2
    failures=$((failures + 1))
  fi
else
  heading "POSTGRESQL"
  printf 'skipped by --no-database\n'
fi

# --- staged imports ---------------------------------------------------------------------------------
if [ "$with_imports" = "true" ]; then
  heading "STAGED IMPORTS (F010, inspection only)"
  if ! $compose run --rm --no-deps backend python scripts/import_staging_reclaim.py; then
    printf '%s: the staged-import inspection failed\n' "$program" >&2
    failures=$((failures + 1))
  fi
else
  heading "STAGED IMPORTS (F010, inspection only)"
  printf 'skipped by --no-imports\n'
fi

heading "RESULT"
if [ "$failures" -eq 0 ]; then
  printf 'complete\n'
  exit 0
fi
# A partial answer must never read as a healthy one.
printf 'PARTIAL: %s section(s) could not be read; totals above are incomplete\n' "$failures"
exit 1
