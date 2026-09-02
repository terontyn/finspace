#!/bin/sh
# Build-set contract: inventory correctness, path confinement, and refusal of anything unproven.
#
# The database verification contract is exercised through a fake /scripts/verify-backup.sh, so this
# test proves the wiring and the refusals without needing PostgreSQL. The real verification path is
# covered by the existing backup integration procedure.
set -eu

fail() {
  printf 'backup-set test: FAIL: %s\n' "$1" >&2
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

# A sandbox whose layout mirrors the container: /backups, /scripts, and the tools on PATH.
sandbox="$test_root/sandbox"
mkdir -p "$sandbox/backups/database" "$sandbox/scripts" "$sandbox/bin"
cp "$repository_root/scripts/backup-set.sh" "$sandbox/scripts/backup-set.sh"

cat >"$sandbox/bin/pg_restore" <<'STUB'
#!/bin/sh
exit 0
STUB
cat >"$sandbox/bin/verify-backup-outcome" <<'STUB'
#!/bin/sh
exit 0
STUB
chmod 755 "$sandbox/bin/pg_restore" "$sandbox/bin/verify-backup-outcome"

# The build-set script calls the real verification entrypoint by absolute path; the sandbox supplies
# a stand-in whose exit status is controlled by a marker file.
cat >"$sandbox/scripts/verify-backup.sh" <<'STUB'
#!/bin/sh
[ "${BACKUP_REMOTE_AFTER_VERIFY:-true}" = "false" ] || {
  echo "verify stub: the secondary copy must be suppressed" >&2
  exit 9
}
[ -n "${1:-}" ] || { echo "verify stub: an explicit dump path is required" >&2; exit 9; }
printf '%s\n' "$1" >>"$VERIFY_LOG"
[ ! -f "$VERIFY_FAIL" ] || exit 1
exit 0
STUB
chmod 755 "$sandbox/scripts/verify-backup.sh"

# Without root, /backups cannot be created. Rewrite the two absolute roots into the sandbox instead;
# this is the only place the test deviates from the shipped script.
sed \
  -e "s#/scripts/verify-backup.sh#$sandbox/scripts/verify-backup.sh#" \
  -e "s#/backups#$sandbox/backups#g" \
  "$repository_root/scripts/backup-set.sh" >"$sandbox/scripts/backup-set-local.sh"
chmod 755 "$sandbox/scripts/backup-set-local.sh"

build_set() {
  PATH="$sandbox/bin:/usr/bin:/bin" \
  VERIFY_LOG="$sandbox/verify.log" \
  VERIFY_FAIL="$sandbox/verify.fail" \
  BACKUP_STORAGE_PATH="$sandbox/backups/database" \
  FINSPACE_COMMIT="${COMMIT_OVERRIDE-0123456789abcdef0123456789abcdef01234567}" \
  FINSPACE_TAG="${TAG_OVERRIDE-local-v0.15}" \
  sh "$sandbox/scripts/backup-set-local.sh" "$@"
}

make_dump() {
  name=$1
  content=$2
  revision=$3
  dump="$sandbox/backups/database/$name"
  printf '%s' "$content" >"$dump"
  sha=$(sha256sum "$dump" | awk '{print $1}')
  size=$(stat -c %s "$dump")
  printf '{\n  "filename": "%s",\n  "sha256": "%s",\n  "created_at": "2026-09-03T01:00:00Z",\n  "database": "finspace",\n  "alembic_revision": "%s",\n  "format": "postgresql-custom",\n  "size_bytes": %s\n}\n' \
    "$name" "$sha" "$revision" "$size" >"$dump.manifest.json"
  printf '%s' "$sha"
}

set_id="2026-09-03T010000Z"
dump_name="finspace_${set_id}.dump"
dump_sha=$(make_dump "$dump_name" "example dump payload" "0017_categorization_history")
dump_path="$sandbox/backups/database/$dump_name"

# --- a valid run produces exactly one inventory and one report -------------------------------
build_set "$dump_path" >/dev/null || fail "a valid dump was refused"
manifest="$sandbox/backups/sets/$set_id/backup-set.json"
report="$sandbox/backups/sets/$set_id/backup-set-report.json"
[ -s "$manifest" ] || fail "backup-set.json was not created"
[ -s "$report" ] || fail "backup-set-report.json was not created"

manifest_text=$(cat "$manifest")
assert_contains "$manifest_text" "\"set_id\": \"$set_id\"" "set id is not recorded"
assert_contains "$manifest_text" "\"sha256\": \"$dump_sha\"" "database sha256 is not recorded"
assert_contains "$manifest_text" '"alembic_revision": "0017_categorization_history"' "revision missing"
assert_contains "$manifest_text" '"size_bytes": 20' "database size is not recorded"
assert_contains "$manifest_text" "\"path\": \"database/$dump_name\"" "database path is not relative"
assert_contains "$manifest_text" '"finspace_tag": "local-v0.15"' "tag is not recorded"
assert_contains "$manifest_text" '"finspace_commit": "0123456789abcdef0123456789abcdef01234567"' "commit missing"
assert_contains "$manifest_text" '"included": false' "n8n must be excluded by default"
case "$manifest_text" in
  */backups/*) fail "the inventory leaked an absolute backup path" ;;
esac

report_text=$(cat "$report")
assert_contains "$report_text" '"local_verified": true' "local verification was not recorded"
assert_contains "$report_text" '"offhost_verified": false' "off-host must start false"
assert_contains "$report_text" '"offhost_destination_label": null' "destination label must start null"
assert_contains "$report_text" '"error": null' "a successful run must record no error"

# The explicit dump path reached the verification contract, not "whatever sorts last".
assert_equal "$dump_path" "$(tail -n 1 "$sandbox/verify.log")" "verification target"

# Permission bits are asserted only where the filesystem can represent them; a Windows checkout
# reports 644 for everything, which says nothing about the Linux production host.
probe="$test_root/mode-probe"
: >"$probe"
chmod 600 "$probe" 2>/dev/null || true
if [ "$(stat -c %a "$probe" 2>/dev/null)" = "600" ]; then
  assert_equal "600" "$(stat -c %a "$manifest")" "inventory permissions"
  assert_equal "600" "$(stat -c %a "$report")" "report permissions"
  assert_equal "700" "$(stat -c %a "$sandbox/backups/sets/$set_id")" "set directory permissions"
else
  printf 'backup-set test: SKIP permission assertions (filesystem ignores chmod)
'
fi

# No secret material may ever be pulled into a set.
if grep -rqiE "password|secret|jwt|encryption_key" "$sandbox/backups/sets/$set_id"; then
  fail "the backup set referenced secret material"
fi
[ ! -e "$sandbox/backups/sets/$set_id/.env" ] || fail ".env was copied into the set"

# --- re-running against identical content is idempotent, conflicting content is refused ------
build_set "$dump_path" >/dev/null || fail "an identical rebuild was refused"
printf 'tampered' >"$dump_path"
if build_set "$dump_path" >/dev/null 2>&1; then
  fail "a dump whose SHA-256 no longer matches its manifest was accepted"
fi
conflict_sha=$(make_dump "$dump_name" "different payload entirely" "0017_categorization_history")
[ "$conflict_sha" != "$dump_sha" ] || fail "test fixture did not change the digest"
if build_set "$dump_path" >/dev/null 2>&1; then
  fail "a conflicting set id was silently overwritten"
fi
assert_contains "$(cat "$manifest")" "\"sha256\": \"$dump_sha\"" "the original inventory was mutated"

# --- refusals ---------------------------------------------------------------------------------
second_id="2026-09-04T010000Z"
second_name="finspace_${second_id}.dump"
make_dump "$second_name" "second payload" "0017_categorization_history" >/dev/null
second_path="$sandbox/backups/database/$second_name"

rm "$second_path.manifest.json"
if build_set "$second_path" >/dev/null 2>&1; then
  fail "a dump without a manifest was accepted"
fi
make_dump "$second_name" "second payload" "0017_categorization_history" >/dev/null

if build_set "/etc/passwd" >/dev/null 2>&1; then
  fail "a path outside the backup directory was accepted"
fi
if build_set "$sandbox/backups/database/../../etc/finspace_x.dump" >/dev/null 2>&1; then
  fail "a traversing path was accepted"
fi

badly_named="$sandbox/backups/database/finspace_not-a-timestamp.dump"
printf 'payload' >"$badly_named"
printf '{"sha256": "%s", "alembic_revision": "0017_categorization_history"}\n' \
  "$(sha256sum "$badly_named" | awk '{print $1}')" >"$badly_named.manifest.json"
if build_set "$badly_named" >/dev/null 2>&1; then
  fail "an unsafe set id was accepted"
fi

if COMMIT_OVERRIDE="" build_set "$second_path" >/dev/null 2>&1; then
  fail "a missing FINSPACE_COMMIT was accepted"
fi
if COMMIT_OVERRIDE="not-hex-at-all" build_set "$second_path" >/dev/null 2>&1; then
  fail "a non-hexadecimal FINSPACE_COMMIT was accepted"
fi
if TAG_OVERRIDE='v1.0; rm -rf /' build_set "$second_path" >/dev/null 2>&1; then
  fail "an unsafe FINSPACE_TAG was accepted"
fi

# An untagged HEAD is legitimate and must record null rather than an empty string.
TAG_OVERRIDE="" build_set "$second_path" >/dev/null || fail "an untagged build was refused"
assert_contains "$(cat "$sandbox/backups/sets/$second_id/backup-set.json")" '"finspace_tag": null' \
  "an untagged build must record null"

# --- failed verification leaves evidence, not a silent success --------------------------------
third_id="2026-09-05T010000Z"
third_name="finspace_${third_id}.dump"
make_dump "$third_name" "third payload" "0017_categorization_history" >/dev/null
: >"$sandbox/verify.fail"
if build_set "$sandbox/backups/database/$third_name" >/dev/null 2>&1; then
  fail "a set whose database verification failed exited zero"
fi
third_report="$sandbox/backups/sets/$third_id/backup-set-report.json"
assert_contains "$(cat "$third_report")" '"local_verified": false' "a failed verification was hidden"
rm "$sandbox/verify.fail"

# No partial files survive any of the runs above.
if find "$sandbox/backups/sets" -name '*.partial' | grep -q .; then
  fail "a .partial artifact survived"
fi

printf 'backup-set test: PASS\n'
