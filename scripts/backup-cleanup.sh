#!/bin/sh
set -eu

backup_dir="${BACKUP_STORAGE_PATH:-/backups/database}"
case "$backup_dir" in
  /backups|/backups/*) ;;
  *) echo "Cleanup refused: BACKUP_STORAGE_PATH must stay inside /backups." >&2; exit 1 ;;
esac
daily="${BACKUP_RETENTION_DAILY:-7}"
weekly="${BACKUP_RETENTION_WEEKLY:-4}"

case "$daily:$weekly" in
  *[!0-9:]*) echo "Retention values must be non-negative integers." >&2; exit 1 ;;
esac

mkdir -p "$backup_dir"
dump_list="$(find "$backup_dir" -maxdepth 1 -type f -name 'finspace_*.dump' -printf '%f\n' | sort -r)"
count="$(printf '%s\n' "$dump_list" | sed '/^$/d' | wc -l)"
if [ "$count" -le 1 ]; then
  echo "Cleanup skipped: at least one usable backup must be retained."
  exit 0
fi

keep_file="$(mktemp)"
trap 'rm -f -- "$keep_file"' EXIT HUP INT TERM

index=0
weeks_kept=0
seen_weeks="|"
printf '%s\n' "$dump_list" | while IFS= read -r filename; do
  [ -n "$filename" ] || continue
  index=$((index + 1))
  if [ "$index" -le "$daily" ] || [ "$index" -eq 1 ]; then
    printf '%s\n' "$filename" >>"$keep_file"
    continue
  fi

  if [ "$weeks_kept" -lt "$weekly" ]; then
    stamp="$(printf '%s' "$filename" | sed -n 's/^finspace_\([0-9-]*T[0-9]*Z\)\.dump$/\1/p')"
    week="$(date -u -d "$stamp" +%G-%V 2>/dev/null || true)"
    if [ -n "$week" ] && ! printf '%s' "$seen_weeks" | grep -Fq "|$week|"; then
      printf '%s\n' "$filename" >>"$keep_file"
      seen_weeks="${seen_weeks}${week}|"
      weeks_kept=$((weeks_kept + 1))
    fi
  fi
done

removed=0
printf '%s\n' "$dump_list" | while IFS= read -r filename; do
  [ -n "$filename" ] || continue
  if ! grep -Fxq "$filename" "$keep_file"; then
    rm -f -- "$backup_dir/$filename" "$backup_dir/$filename.manifest.json"
    removed=$((removed + 1))
  fi
done

echo "Backup cleanup completed; retained daily=${daily}, weekly=${weekly}, total before cleanup=${count}."
