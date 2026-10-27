#!/bin/sh
# Host-side orchestration for an optional COLD n8n archive.
#
# n8n stores its state in SQLite inside a Docker volume, so a live copy is not safe. This stops
# only n8n, archives the volume through a profile=tools helper, and restarts n8n only if it was
# running beforehand. PostgreSQL, Redis, backend, frontend and both workers are never touched:
# the financial backup stays online-safe.
#
# n8n is optional. Core Finspace restore never depends on this artifact.
set -eu

umask 077

usage() {
  echo "Usage: n8n-archive.sh SET_ID" >&2
}

[ "$#" -eq 1 ] || { usage; exit 2; }
set_id="$1"
case "$set_id" in
  ????-??-??T??????Z) ;;
  *) echo "n8n archive refused: unsafe set id." >&2; exit 1 ;;
esac
case "$set_id" in
  *[!0-9TZ-]*) echo "n8n archive refused: unsafe set id." >&2; exit 1 ;;
esac

compose="${FINSPACE_COMPOSE:-docker compose}"

was_running="false"
if $compose ps --status running --services 2>/dev/null | grep -qx n8n; then
  was_running="true"
fi

restore_previous_state() {
  # Never leave a previously running n8n stopped because the archive failed.
  if [ "$was_running" = "true" ]; then
    $compose start n8n >/dev/null 2>&1 || \
      echo "n8n archive warning: n8n could not be restarted; start it manually." >&2
  fi
}

if [ "$was_running" = "true" ]; then
  trap 'restore_previous_state' EXIT HUP INT TERM
  $compose stop n8n >/dev/null
fi

$compose --profile tools run --rm n8n-backup sh /scripts/n8n-archive-volume.sh "$set_id"

if [ "$was_running" = "true" ]; then
  trap - EXIT HUP INT TERM
  restore_previous_state
  echo "n8n archived for set $set_id; n8n restarted."
else
  echo "n8n archived for set $set_id; n8n was already stopped and stays stopped."
fi
