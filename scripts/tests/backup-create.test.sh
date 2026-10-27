#!/bin/sh
# What backup.sh records about the dump it just made.
#
# The manifest stays part of the restore artifact, but it lives in a 0700 root-owned directory the
# non-root backend cannot read. The audit event is therefore the operational read model, and it has
# to carry the same Alembic revision — captured once, before the dump, and never re-queried.
set -eu

fail() {
  printf 'backup-create test: FAIL: %s\n' "$1" >&2
  exit 1
}

assert_equal() {
  [ "$2" = "$1" ] || fail "$3: expected [$1], got [$2]"
}

assert_contains() {
  case "$1" in
    *"$2"*) ;;
    *) fail "$3" ;;
  esac
}

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

bin="$test_root/bin"
backups="$test_root/backups"
mkdir -p "$bin" "$backups/database"

revision="0017_categorization_history"

# psql answers the revision query and records everything else verbatim, so the audit statement and
# its bound values can both be inspected.
cat >"$bin/psql" <<'STUB'
#!/bin/sh
printf 'psql %s\n' "$*" >>"$PSQL_LOG"
case "$*" in
  *"SELECT version_num FROM alembic_version"*)
    printf 'REVISION_QUERY\n' >>"$REVISION_QUERY_LOG"
    printf '%s\n' "$FAKE_REVISION"
    exit 0
    ;;
esac
# Heredoc statements arrive on stdin; keep them next to their bound -v values.
cat >>"$PSQL_LOG" 2>/dev/null || true
exit 0
STUB
cat >"$bin/pg_isready" <<'STUB'
#!/bin/sh
exit 0
STUB
cat >"$bin/pg_dump" <<'STUB'
#!/bin/sh
target=""
for argument in "$@"; do
  case "$argument" in
    --file=*) target="${argument#--file=}" ;;
  esac
done
printf 'custom-format dump payload' >"$target"
exit 0
STUB
cat >"$bin/pg_restore" <<'STUB'
#!/bin/sh
exit 0
STUB
chmod 755 "$bin/psql" "$bin/pg_isready" "$bin/pg_dump" "$bin/pg_restore"

# The script hard-codes /backups; the sandbox stands in for it, which is the only deviation here.
sed -e "s#/backups#$backups#g" "$repository_root/scripts/backup.sh" >"$test_root/backup.sh"

: >"$test_root/psql.log"
: >"$test_root/revision.log"
output=$(
  PATH="$bin:/usr/bin:/bin" \
  PSQL_LOG="$test_root/psql.log" \
  REVISION_QUERY_LOG="$test_root/revision.log" \
  FAKE_REVISION="$revision" \
  PGDATABASE=finspace \
  BACKUP_STORAGE_PATH="$backups/database" \
  sh "$test_root/backup.sh" 2>&1
) || fail "a normal backup run failed"

dump_path=$(printf '%s\n' "$output" | tail -n 1)
[ -s "$dump_path" ] || fail "no dump was produced"
dump_name=$(basename "$dump_path")
manifest="$dump_path.manifest.json"
[ -s "$manifest" ] || fail "no manifest was produced"
sha=$(sha256sum "$dump_path" | awk '{print $1}')

# The manifest is unchanged: it is still part of the restore artifact.
assert_contains "$(cat "$manifest")" "\"alembic_revision\": \"$revision\"" "manifest revision"
assert_contains "$(cat "$manifest")" "\"sha256\": \"$sha\"" "manifest digest"
assert_contains "$(cat "$manifest")" '"format": "postgresql-custom"' "manifest format"

log=$(cat "$test_root/psql.log")

# The audit event now carries the revision alongside the identity of the dump.
assert_contains "$log" "backup.created" "the backup.created event was not written"
assert_contains "$log" "'alembic_revision', :'alembic_revision'" "the event omits the revision"
assert_contains "$log" "'filename', :'filename'" "the event omits the filename"
assert_contains "$log" "'sha256', :'sha256'" "the event omits the digest"
assert_contains "$log" "'size_bytes', :'size_bytes'" "the event omits the size"
# ...and the bound values are the ones this run actually produced.
assert_contains "$log" "-v alembic_revision=$revision" "the recorded revision is not the captured one"
assert_contains "$log" "-v filename=$dump_name" "the recorded filename is not this dump"
assert_contains "$log" "-v sha256=$sha" "the recorded digest is not this dump"

# Exactly one revision query: the value written to the manifest and to the audit row is the same
# capture, not a second read that could disagree with it.
assert_equal "1" "$(grep -c REVISION_QUERY "$test_root/revision.log")" "revision queries"

# Nothing else about the artifact contract moved.
[ -f "$backups/database/$dump_name" ] || fail "the dump is not in the canonical directory"
[ ! -e "$dump_path.partial" ] || fail "a partial artifact survived"

# A backup whose revision cannot be read is still refused outright.
: >"$test_root/psql.log"
if PATH="$bin:/usr/bin:/bin" \
  PSQL_LOG="$test_root/psql.log" \
  REVISION_QUERY_LOG="$test_root/revision.log" \
  FAKE_REVISION="" \
  PGDATABASE=finspace \
  BACKUP_STORAGE_PATH="$backups/database" \
  sh "$test_root/backup.sh" >/dev/null 2>&1; then
  fail "a backup without an Alembic revision was accepted"
fi

printf 'backup-create test: PASS\n'
