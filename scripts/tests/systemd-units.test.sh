#!/bin/sh
# The scheduler is host systemd, so its units are repository assets and are reviewed like code.
#
# systemd-analyze is used when the environment has it; otherwise the units are validated as text.
# Either way the contract is the same: a one-shot root job, daily at host-local 01:00, catching up
# after downtime, with no dependency on n8n and no touching of application containers.
set -eu

fail() {
  printf 'systemd-units test: FAIL: %s\n' "$1" >&2
  exit 1
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

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
service="$repository_root/infrastructure/systemd/finspace-backup.service"
timer="$repository_root/infrastructure/systemd/finspace-backup.timer"

[ -s "$service" ] || fail "the service unit is missing"
[ -s "$timer" ] || fail "the timer unit is missing"

# Assert on directives, not on the comments that explain them.
service_text=$(grep -v '^[[:space:]]*#' "$service")
timer_text=$(grep -v '^[[:space:]]*#' "$timer")

# --- service ------------------------------------------------------------------------------------
assert_contains "$service_text" "Type=oneshot" "the service is not one-shot"
assert_contains "$service_text" "User=root" "the service does not run as root"
assert_contains "$service_text" "WorkingDirectory=/opt/finspace" "wrong working directory"
assert_contains "$service_text" "EnvironmentFile=/etc/finspace/backup.env" "host config is not loaded"
assert_contains "$service_text" "ExecStart=/opt/finspace/scripts/backup-run.sh" "wrong entrypoint"
assert_contains "$service_text" "TimeoutStartSec=30min" "no bounded timeout"
assert_contains "$service_text" "UMask=0077" "the umask is not restrictive"
assert_contains "$service_text" "StandardOutput=journal" "logs do not go to journald"
assert_contains "$service_text" "StandardError=journal" "errors do not go to journald"

# The backup must never depend on the optional automation stack, and must never restart the app.
assert_absent "$service_text" "n8n" "the service references n8n"
assert_absent "$service_text" "compose restart" "the service restarts containers"
assert_absent "$service_text" "compose up" "the service starts containers"
assert_absent "$service_text" "docker compose restart" "the service restarts containers"
# No unmanaged log file, and no secrets inline.
assert_absent "$service_text" ">>" "the service redirects into a file"
for secret in JWT_SECRET_KEY POSTGRES_PASSWORD GOOGLE_TOKEN_ENCRYPTION_KEY N8N_ENCRYPTION_KEY; do
  assert_absent "$service_text" "$secret" "the service unit names a secret"
done

# --- timer --------------------------------------------------------------------------------------
assert_contains "$timer_text" "OnCalendar=*-*-* 01:00:00" "the daily 01:00 schedule is missing"
assert_contains "$timer_text" "Persistent=true" "a missed run would never catch up"
assert_contains "$timer_text" "RandomizedDelaySec=" "the start is not spread"
assert_contains "$timer_text" "Unit=finspace-backup.service" "the timer targets the wrong unit"
assert_contains "$timer_text" "WantedBy=timers.target" "the timer cannot be enabled"
# Daily by default: anything more frequent would fight BACKUP_STALE_HOURS=36.
assert_absent "$timer_text" "OnUnitActiveSec" "the timer adds a second cadence"
assert_absent "$timer_text" "hourly" "the timer runs more often than daily"

# --- staged import reclamation --------------------------------------------------------------------
# Optional and separate from the backup schedule: it sweeps up what a crash left in data/imports,
# and it must never look like a backup job or touch a running container.
reclaim_service="$repository_root/infrastructure/systemd/finspace-import-reclaim.service"
reclaim_timer="$repository_root/infrastructure/systemd/finspace-import-reclaim.timer"
[ -s "$reclaim_service" ] || fail "the reclamation service unit is missing"
[ -s "$reclaim_timer" ] || fail "the reclamation timer unit is missing"
reclaim_service_text=$(grep -v '^[[:space:]]*#' "$reclaim_service")
reclaim_timer_text=$(grep -v '^[[:space:]]*#' "$reclaim_timer")

assert_contains "$reclaim_service_text" "Type=oneshot" "the reclamation service is not one-shot"
assert_contains "$reclaim_service_text" "WorkingDirectory=/opt/finspace" "wrong working directory"
assert_contains "$reclaim_service_text" "scripts/import_staging_reclaim.py --apply" \
  "the reclamation service does not invoke the reclamation command"
assert_contains "$reclaim_service_text" "run --rm --no-deps backend" \
  "the reclamation service does not use a throwaway backend process"
assert_contains "$reclaim_service_text" "TimeoutStartSec=" "the reclamation service is unbounded"
assert_contains "$reclaim_service_text" "StandardOutput=journal" "reclamation logs bypass journald"
# It must not restart or recreate anything, and must not carry application secrets.
for forbidden in "compose up" "compose restart" "force-recreate" "down" "n8n"; do
  assert_absent "$reclaim_service_text" "$forbidden" \
    "the reclamation service does something to containers: $forbidden"
done
for secret in JWT_SECRET_KEY POSTGRES_PASSWORD GOOGLE_TOKEN_ENCRYPTION_KEY N8N_ENCRYPTION_KEY; do
  assert_absent "$reclaim_service_text" "$secret" "the reclamation unit names a secret"
done
assert_absent "$reclaim_service_text" "EnvironmentFile" \
  "the reclamation service loads host configuration it does not need"

assert_contains "$reclaim_timer_text" "Persistent=true" "a missed reclamation would never catch up"
assert_contains "$reclaim_timer_text" "Unit=finspace-import-reclaim.service" \
  "the reclamation timer targets the wrong unit"
assert_contains "$reclaim_timer_text" "WantedBy=timers.target" "the reclamation timer cannot be enabled"
assert_absent "$reclaim_timer_text" "OnUnitActiveSec" "the reclamation timer adds a second cadence"
assert_absent "$reclaim_timer_text" "hourly" "the reclamation timer runs far more often than needed"
# The two schedules must not collide: reclamation is weekly, the backup is daily at 01:00.
assert_absent "$reclaim_timer_text" "01:00:00" "reclamation collides with the backup window"

if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$service" || fail "systemd-analyze rejected the service unit"
  systemd-analyze verify "$timer" || fail "systemd-analyze rejected the timer unit"
  systemd-analyze verify "$reclaim_service" || fail "systemd-analyze rejected the reclamation service"
  systemd-analyze verify "$reclaim_timer" || fail "systemd-analyze rejected the reclamation timer"
  printf 'systemd-units test: systemd-analyze verified all four units\n'
else
  printf 'systemd-units test: SKIP systemd-analyze (not installed); units validated as text\n'
fi

# --- consumer compatibility -----------------------------------------------------------------------
# Stage B can now report a locally verified but not yet off-host backup as "unverified". The backup
# health workflow alerts on any status other than "healthy", so it handles the new case unchanged.
workflow="$repository_root/n8n/workflows/06-backup-health.json"
[ -s "$workflow" ] || fail "the backup health workflow is missing"
workflow_text=$(cat "$workflow")
assert_contains "$workflow_text" '"value2": "healthy"' "the workflow no longer compares to healthy"
assert_contains "$workflow_text" '"operation": "notEqual"' "the workflow no longer alerts on non-healthy"

printf 'systemd-units test: PASS\n'
