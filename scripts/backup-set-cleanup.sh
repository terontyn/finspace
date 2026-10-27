#!/bin/sh
# Remove local backup sets whose canonical database dump the PostgreSQL retention policy has
# already deleted.
#
# This deliberately implements no calendar policy of its own. backup-cleanup.sh owns the 7-daily /
# 4-weekly decision; a set is nothing but an inventory pointing at one of those dumps, so its
# lifetime simply follows the dump it references. Two policies that could disagree would be worse
# than none.
#
# Nothing here touches remote sets. Remote storage is append-only in Stage B: the real off-host
# target does not exist yet, so a retention policy for it cannot be chosen responsibly.
#
# Anything unexpected — a malformed inventory, a traversing path, an unrecognised directory — is
# reported and LEFT ALONE. An automated deleter that guesses is worse than an operator who looks.
set -eu

umask 077

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"
}

backup_root="${FINSPACE_BACKUP_ROOT:-/opt/finspace/backups}"
sets_root="${backup_root}/sets"
[ -d "$backup_root" ] || { echo "Set cleanup failed: backup root does not exist." >&2; exit 1; }
[ -d "$sets_root" ] || { log "backup_set_cleanup_finished removed=0 retained=0"; exit 0; }

removed=0
retained=0
skipped=0

for set_dir in "$sets_root"/*; do
  [ -d "$set_dir" ] || continue
  set_id="$(basename "$set_dir")"

  # Only directories this project creates are candidates. Partials, dotfiles and anything with an
  # unexpected shape are left for a human.
  case "$set_id" in
    ????-??-??T??????Z) ;;
    *)
      log "backup_set_cleanup_skipped set_id=$set_id reason=unrecognised_directory"
      skipped=$((skipped + 1))
      continue
      ;;
  esac
  case "$set_id" in
    *[!0-9TZ-]*)
      log "backup_set_cleanup_skipped set_id=$set_id reason=unsafe_set_id"
      skipped=$((skipped + 1))
      continue
      ;;
  esac

  manifest="${set_dir}/backup-set.json"
  if [ ! -s "$manifest" ]; then
    log "backup_set_cleanup_skipped set_id=$set_id reason=missing_inventory"
    skipped=$((skipped + 1))
    continue
  fi

  relative="$(sed -n 's/.*"path": *"\([^"]*\)".*/\1/p' "$manifest" | head -n 1)"
  # Traversal is checked first so an escaping path is reported as exactly that, not lumped in with
  # ordinary malformed inventories.
  case "$relative" in
    *..*|/*)
      log "backup_set_cleanup_skipped set_id=$set_id reason=unsafe_inventory_path"
      skipped=$((skipped + 1))
      continue
      ;;
  esac
  case "$relative" in
    database/finspace_*.dump) ;;
    *)
      log "backup_set_cleanup_skipped set_id=$set_id reason=malformed_inventory"
      skipped=$((skipped + 1))
      continue
      ;;
  esac

  if [ -e "${backup_root}/${relative}" ]; then
    retained=$((retained + 1))
    continue
  fi

  # The canonical dump is gone, so the inventory now points at nothing and the set — including any
  # optional n8n archive that belongs to it — can go with it.
  case "$set_dir" in
    "$sets_root"/*) ;;
    *)
      log "backup_set_cleanup_skipped set_id=$set_id reason=outside_backup_root"
      skipped=$((skipped + 1))
      continue
      ;;
  esac
  rm -rf -- "$set_dir"
  removed=$((removed + 1))
  log "backup_set_cleanup_removed set_id=$set_id reason=canonical_dump_expired"
done

log "backup_set_cleanup_finished removed=$removed retained=$retained skipped=$skipped"
