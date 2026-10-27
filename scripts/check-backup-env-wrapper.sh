#!/bin/sh
# Check that the host's scheduled backup can still find the Compose wrapper.
#
# Hosts installed before the wrapper became a repository-managed file can carry an absolute
# FINSPACE_COMPOSE=/usr/local/sbin/finspace-compose in /etc/finspace/backup.env. Retiring the old
# wrapper does not touch that line, so `finspace-backup.service` starts failing while everything
# else looks healthy — the failure is only visible in the journal, and only if somebody looks.
#
# This is a read-only diagnostic. It never writes to backup.env: repairing host configuration from a
# repository script would be a far worse habit than asking the operator to change one line, and the
# operator has to run the backup afterwards anyway to prove the repair worked.
set -eu

usage() {
  echo "Usage: check-backup-env-wrapper.sh [--backup-env FILE] [--wrapper PATH]" >&2
  echo "  exit 0 configuration is usable, 2 usage error, 3 repair required" >&2
}

backup_env="/etc/finspace/backup.env"
wrapper="/usr/local/bin/finspace-compose"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --backup-env) [ "$#" -ge 2 ] || { usage; exit 2; }; backup_env="$2"; shift 2 ;;
    --wrapper) [ "$#" -ge 2 ] || { usage; exit 2; }; wrapper="$2"; shift 2 ;;
    -h|--help) usage; exit 2 ;;
    *) usage; exit 2 ;;
  esac
done

case "$wrapper" in
  /*) ;;
  *) echo "backup.env check: FAIL: --wrapper must be an absolute path" >&2; exit 2 ;;
esac

repair_required() {
  printf 'backup.env check: REPAIR REQUIRED: %s\n' "$1" >&2
  exit 3
}

if [ ! -x "$wrapper" ]; then
  repair_required "the canonical wrapper $wrapper is missing or not executable; install it with
sudo ./scripts/install-finspace-compose.sh"
fi

# A host that has never configured scheduled backups has nothing to repair.
if [ ! -r "$backup_env" ]; then
  printf 'backup.env check: PASS: %s is absent, wrapper %s is in place\n' "$backup_env" "$wrapper"
  exit 0
fi

# Last assignment wins, the same way systemd's EnvironmentFile reads it.
configured=$(sed -n 's/^[[:space:]]*FINSPACE_COMPOSE=//p' "$backup_env" | tail -n 1)
configured=$(printf '%s' "$configured" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//")

case "$configured" in
  # Unset, or a bare command name resolved through PATH. Both reach the wrapper wherever it was
  # installed, which is what the documented configuration says; only an absolute pin can go stale.
  '')
    printf 'backup.env check: PASS: FINSPACE_COMPOSE is unset, the built-in default applies\n'
    exit 0
    ;;
  /*) ;;
  *)
    printf 'backup.env check: PASS: FINSPACE_COMPOSE=%s resolves through PATH\n' "$configured"
    exit 0
    ;;
esac

if [ "$configured" = "$wrapper" ]; then
  printf 'backup.env check: PASS: FINSPACE_COMPOSE=%s\n' "$configured"
  exit 0
fi

if [ -x "$configured" ]; then
  repair_required "$backup_env pins FINSPACE_COMPOSE=$configured, but the canonical wrapper is
$wrapper. Set FINSPACE_COMPOSE=$wrapper (or the bare name finspace-compose), then run the backup
once before declaring the upgrade complete."
fi
repair_required "$backup_env pins FINSPACE_COMPOSE=$configured, which does not exist: the scheduled
backup will fail. Set FINSPACE_COMPOSE=$wrapper, then run finspace-backup.service once before
declaring the upgrade complete."
