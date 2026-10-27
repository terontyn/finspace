#!/bin/sh
# Archive the n8n state volume as opaque bytes. Runs inside the profile=tools helper, which mounts
# the volume read-only; the caller is responsible for having stopped n8n first.
#
# The volume is copied wholesale rather than exported: `n8n export:credentials` writes DECRYPTED
# credentials, which must never reach a backup artifact. Encrypted credentials stay encrypted here,
# and are useless without the separately custodied N8N_ENCRYPTION_KEY.
set -eu

umask 077

usage() {
  echo "Usage: n8n-archive-volume.sh SET_ID" >&2
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

source_dir="${N8N_SOURCE_DIR:-/source}"
[ -d "$source_dir" ] || { echo "n8n archive failed: source volume is not mounted." >&2; exit 1; }

set_dir="/backups/sets/${set_id}"
mkdir -p "$set_dir"
chmod 700 /backups/sets "$set_dir" 2>/dev/null || true

archive="${set_dir}/n8n-data.tar.gz"
digest="${set_dir}/n8n-data.sha256"
partial="${archive}.partial"
digest_partial="${digest}.partial"
cleanup_partial() {
  rm -f -- "$partial" "$digest_partial"
}
trap cleanup_partial EXIT HUP INT TERM

[ ! -e "$archive" ] || { echo "n8n archive refused: $archive already exists." >&2; exit 1; }

# Deterministic ordering and no absolute paths, so the archive restores predictably into a volume.
tar --create --gzip --file="$partial" --directory="$source_dir" --sort=name .
[ -s "$partial" ] || { echo "n8n archive failed: produced an empty archive." >&2; exit 1; }
tar --list --file="$partial" >/dev/null

sha256sum "$partial" | awk '{print $1}' >"$digest_partial"
mv "$partial" "$archive"
mv "$digest_partial" "$digest"
chmod 600 "$archive" "$digest" 2>/dev/null || true
trap - EXIT HUP INT TERM

echo "n8n volume archived for set $set_id ($(stat -c %s "$archive") bytes)."
