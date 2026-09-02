#!/bin/sh
# Record that a verified backup set reached its off-host destination.
#
# Reuses the existing `backup.remote.copy` audit action written by backup-secondary-copy.sh, so no
# new table and no migration are involved. Stage B correlates this row with `backup.created` by
# SHA-256 to decide whether a backup is genuinely safe.
#
# A failed transfer must never reach this script: it is invoked only after remote hash verification
# and atomic publication have both succeeded.
set -eu

usage() {
  echo "Usage: backup-remote-audit.sh DUMP_FILENAME SHA256 DESTINATION_LABEL" >&2
}

[ "$#" -eq 3 ] || { usage; exit 2; }
filename="$1"
sha256="$2"
label="$3"

case "$filename" in
  finspace_*.dump) ;;
  *) echo "Audit refused: unexpected dump filename." >&2; exit 1 ;;
esac
case "$filename" in
  */*|*..*) echo "Audit refused: filename must not contain a path." >&2; exit 1 ;;
esac
[ ${#sha256} -eq 64 ] || { echo "Audit refused: malformed SHA-256." >&2; exit 1; }
case "$sha256" in
  *[!0-9a-f]*) echo "Audit refused: malformed SHA-256." >&2; exit 1 ;;
esac
# Only a short opaque label is recorded: never the host, user, key path or remote directory.
case "$label" in
  ''|*[!A-Za-z0-9._-]*) echo "Audit refused: unsafe destination label." >&2; exit 1 ;;
esac

psql -Xq -v filename="$filename" -v sha256="$sha256" -v label="$label" <<'SQL'
INSERT INTO audit_log (
  id, workspace_id, actor_user_id, entity_type, entity_id, action,
  before_data, after_data, request_id, source
)
VALUES (
  gen_random_uuid(), NULL, NULL, 'backup', gen_random_uuid(), 'backup.remote.copy',
  NULL,
  jsonb_build_object('filename', :'filename', 'sha256', :'sha256', 'destination_label', :'label'),
  NULL, 'system'
);
SQL

echo "Recorded backup.remote.copy for $filename."
