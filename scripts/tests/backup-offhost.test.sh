#!/bin/sh
# Off-host transport contract, driven through fake ssh/rsync/docker binaries.
#
# What matters here is not that files move, but that an interrupted or corrupted transfer can never
# look like a completed remote backup, that the local backup always survives, and that no run ever
# reaches the network with unpinned host keys or a readable private key.
set -eu

fail() {
  printf 'backup-offhost test: FAIL: %s\n' "$1" >&2
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
backup_root="$test_root/backups"
remote_root="$test_root/remote"
mkdir -p "$bin" "$backup_root/database" "$remote_root"

# The fake remote is a local directory tree; ssh and rsync operate on it so atomic publication and
# remote checksum verification are genuinely exercised.
cat >"$bin/ssh" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$SSH_LOG"
command=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-i) shift 2 ;;
    -*) shift ;;
    *@*) shift ;;
    *) command="$*"; break ;;
  esac
done
[ ! -f "$SSH_FAIL" ] || exit 255
sh -c "$command"
STUB
cat >"$bin/rsync" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$RSYNC_LOG"
[ ! -f "$RSYNC_FAIL" ] || exit 12
source=""
target=""
for argument in "$@"; do
  case "$argument" in
    -*|ssh*) ;;
    *:*) target="${argument#*:}" ;;
    *) source="$argument" ;;
  esac
done
mkdir -p "$target"
cp -R "$source". "$target" 2>/dev/null || cp -R "$source"/. "$target"
[ ! -f "$CORRUPT_TRANSFER" ] || printf 'corrupted' >"$target/database.dump"
exit 0
STUB
# The fake docker records what the world looked like AT AUDIT TIME, which is how ordering is
# proven: the report must not yet claim success, and the remote set must already be published.
cat >"$bin/docker" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$DOCKER_LOG"
case "$*" in
  *backup-remote-audit.sh*)
    grep -o '"offhost_verified": [a-z]*' "$REPORT_PATH" | head -n 1 | awk '{print $2}' >"$AUDIT_SAW_REPORT"
    if [ -d "$FINAL_PATH" ]; then
      printf 'yes\n' >"$AUDIT_SAW_FINAL"
    else
      printf 'no\n' >"$AUDIT_SAW_FINAL"
    fi
    [ ! -f "$AUDIT_FAIL" ] || exit 1
    ;;
esac
exit 0
STUB
chmod 755 "$bin/ssh" "$bin/rsync" "$bin/docker"

ssh_key="$test_root/id_backup"
known_hosts="$test_root/known_hosts"
printf 'PRIVATE KEY PLACEHOLDER\n' >"$ssh_key"
chmod 600 "$ssh_key"
printf 'backup.example.invalid ssh-ed25519 AAAA\n' >"$known_hosts"

# The permission refusal can only be tested where chmod is honoured. One reading of one mode cannot
# establish that: whichever value is probed, some umask produces it for free, and a filesystem that
# ignores chmod entirely then looks capable and the refusal case fails for the wrong reason. Probe
# both directions instead — only a filesystem that really applies chmod can do both.
chmod_supported="false"
chmod_probe="$test_root/chmod_probe"
printf 'probe\n' >"$chmod_probe"
chmod 600 "$chmod_probe" 2>/dev/null || true
tightened=$(stat -c %a "$chmod_probe" 2>/dev/null || true)
chmod 644 "$chmod_probe" 2>/dev/null || true
loosened=$(stat -c %a "$chmod_probe" 2>/dev/null || true)
if [ "$tightened" = "600" ] && [ "$loosened" = "644" ]; then
  chmod_supported="true"
fi
rm -f "$chmod_probe"

set_id="2026-09-03T010000Z"
dump_name="finspace_${set_id}.dump"
dump_path="$backup_root/database/$dump_name"
printf 'example dump payload' >"$dump_path"
dump_sha=$(sha256sum "$dump_path" | awk '{print $1}')
printf '{"filename": "%s", "sha256": "%s", "alembic_revision": "0017_categorization_history"}\n' \
  "$dump_name" "$dump_sha" >"$dump_path.manifest.json"
manifest_sha=$(sha256sum "$dump_path.manifest.json" | awk '{print $1}')

set_dir="$backup_root/sets/$set_id"
mkdir -p "$set_dir"
write_set() {
  printf '{\n  "version": 1,\n  "set_id": "%s",\n  "created_at": "2026-09-03T01:00:00Z",\n  "finspace_commit": "0123456789abcdef0123456789abcdef01234567",\n  "finspace_tag": "local-v0.15",\n  "alembic_revision": "0017_categorization_history",\n  "database": {\n      "path": "database/%s",\n      "sha256": "%s",\n      "manifest_sha256": "%s",\n      "size_bytes": 20\n    },\n  "n8n": {\n      "included": false,\n      "path": null,\n      "sha256": null,\n      "size_bytes": null\n    }\n}\n' \
    "$set_id" "$dump_name" "$dump_sha" "$manifest_sha" >"$set_dir/backup-set.json"
}
write_report() {
  printf '{\n  "version": 1,\n  "set_id": "%s",\n  "created_at": "2026-09-03T01:00:00Z",\n  "local_verified": %s,\n  "local_verified_at": "2026-09-03T01:05:00Z",\n  "offhost_verified": false,\n  "offhost_verified_at": null,\n  "offhost_destination_label": null,\n  "error": null\n}\n' \
    "$set_id" "$1" >"$set_dir/backup-set-report.json"
}
write_set
write_report true

run_offhost() {
  PATH="$bin:/usr/bin:/bin" \
  SSH_LOG="$test_root/ssh.log" \
  RSYNC_LOG="$test_root/rsync.log" \
  DOCKER_LOG="$test_root/docker.log" \
  SSH_FAIL="$test_root/ssh.fail" \
  RSYNC_FAIL="$test_root/rsync.fail" \
  CORRUPT_TRANSFER="$test_root/corrupt" \
  AUDIT_FAIL="$test_root/audit.fail" \
  AUDIT_SAW_REPORT="$test_root/audit-saw-report" \
  AUDIT_SAW_FINAL="$test_root/audit-saw-final" \
  REPORT_PATH="$set_dir/backup-set-report.json" \
  FINAL_PATH="$remote_root/finspace/sets/$set_id" \
  FINSPACE_BACKUP_ROOT="$backup_root" \
  FINSPACE_BACKUP_REMOTE_HOST="${HOST_OVERRIDE-backup.example.invalid}" \
  FINSPACE_BACKUP_REMOTE_USER="finspace-backup" \
  FINSPACE_BACKUP_REMOTE_ROOT="$remote_root" \
  FINSPACE_BACKUP_SSH_KEY="${KEY_OVERRIDE-$ssh_key}" \
  FINSPACE_BACKUP_KNOWN_HOSTS="${HOSTS_OVERRIDE-$known_hosts}" \
  FINSPACE_BACKUP_REMOTE_LABEL="nas" \
  sh "$repository_root/scripts/backup-offhost.sh" "$@"
}

reset_logs() {
  : >"$test_root/ssh.log"
  : >"$test_root/rsync.log"
  : >"$test_root/docker.log"
  rm -f "$test_root/audit-saw-report" "$test_root/audit-saw-final"
}

# --- A: successful copy ------------------------------------------------------------------------
reset_logs
run_offhost "$set_id" >/dev/null || fail "a valid off-host copy failed"

final="$remote_root/finspace/sets/$set_id"
[ -d "$final" ] || fail "the final remote set was not published"
[ -s "$final/database.dump" ] || fail "the remote set has no database dump"
[ -s "$final/database.manifest.json" ] || fail "the remote set has no database manifest"
[ -s "$final/backup-set.json" ] || fail "the remote set has no inventory"
[ -s "$final/backup-set-report.json" ] || fail "the remote set has no report"
[ -s "$final/SHA256SUMS" ] || fail "the remote set has no checksum list"
assert_equal "$dump_sha" "$(sha256sum "$final/database.dump" | awk '{print $1}')" "remote dump digest"
[ ! -d "$remote_root/finspace/sets/.$set_id.partial" ] || fail "a partial directory survived"

ssh_log=$(cat "$test_root/ssh.log")
assert_contains "$ssh_log" "BatchMode=yes" "BatchMode was not enforced"
assert_contains "$ssh_log" "StrictHostKeyChecking=yes" "host key checking was not enforced"
assert_contains "$ssh_log" "UserKnownHostsFile=$known_hosts" "known_hosts was not pinned"
assert_contains "$ssh_log" "sha256sum -c SHA256SUMS" "the remote checksums were not verified"
assert_contains "$ssh_log" "mv " "the publication was not a rename"
case "$ssh_log" in
  *"StrictHostKeyChecking=no"*) fail "host key checking was disabled" ;;
  *"sshpass"*) fail "sshpass was used" ;;
  *"PRIVATE KEY PLACEHOLDER"*) fail "private key material reached the command line" ;;
esac

# The audit row is recorded exactly once, and only after publication.
audit_calls=$(grep -c "backup-remote-audit.sh" "$test_root/docker.log" || true)
assert_equal "1" "$audit_calls" "backup.remote.copy audit invocations"
assert_contains "$(cat "$test_root/docker.log")" "$dump_sha" "the audit did not carry the dump digest"
assert_contains "$(cat "$test_root/docker.log")" "nas" "the audit did not carry the destination label"
case "$(cat "$test_root/docker.log")" in
  *"$ssh_key"*) fail "the audit leaked the SSH key path" ;;
  *"$remote_root"*) fail "the audit leaked the remote path" ;;
esac

report=$(cat "$set_dir/backup-set-report.json")
assert_contains "$report" '"offhost_verified": true' "the report did not record the off-host copy"
assert_contains "$report" '"offhost_destination_label": "nas"' "the destination label was not recorded"
assert_contains "$report" '"local_verified": true' "local verification was lost"
case "$report" in
  *"$remote_root"*) fail "the report leaked the remote path" ;;
esac
# The immutable inventory is never rewritten by transport.
case "$(cat "$set_dir/backup-set.json")" in
  *offhost*|*local_verified*) fail "the immutable inventory gained run evidence" ;;
esac

# --- G: an existing final remote set is refused, never overwritten ------------------------------
reset_logs
if run_offhost "$set_id" >/dev/null 2>&1; then
  fail "an existing remote set was overwritten"
fi
assert_equal "$dump_sha" "$(sha256sum "$final/database.dump" | awk '{print $1}')" "remote set after refusal"

# --- B: rsync failure ---------------------------------------------------------------------------
rm -rf "$remote_root/finspace"
write_report true
reset_logs
: >"$test_root/rsync.fail"
if run_offhost "$set_id" >/dev/null 2>&1; then
  fail "a failed transfer exited zero"
fi
rm "$test_root/rsync.fail"
[ ! -d "$remote_root/finspace/sets/$set_id" ] || fail "a failed transfer published a final set"
assert_equal "0" "$(grep -c 'backup-remote-audit.sh' "$test_root/docker.log" || true)" "audit after failure"
[ -s "$dump_path" ] || fail "the local dump was disturbed by a failed transfer"
assert_equal "$dump_sha" "$(sha256sum "$dump_path" | awk '{print $1}')" "local dump digest after failure"

# --- C: remote checksum mismatch ----------------------------------------------------------------
rm -rf "$remote_root/finspace"
write_report true
reset_logs
: >"$test_root/corrupt"
if run_offhost "$set_id" >/dev/null 2>&1; then
  fail "a corrupted transfer exited zero"
fi
rm "$test_root/corrupt"
[ ! -d "$remote_root/finspace/sets/$set_id" ] || fail "a corrupted transfer published a final set"
[ ! -d "$remote_root/finspace/sets/.$set_id.partial" ] || fail "a corrupted partial was left behind"
assert_equal "0" "$(grep -c 'backup-remote-audit.sh' "$test_root/docker.log" || true)" "audit after mismatch"
assert_contains "$(cat "$set_dir/backup-set-report.json")" '"offhost_verified": false' \
  "a corrupted transfer was reported as verified"

# --- D/E/F: refusals before any network command --------------------------------------------------
rm -rf "$remote_root/finspace"
write_report true

reset_logs
if HOSTS_OVERRIDE="$test_root/missing_known_hosts" run_offhost "$set_id" >/dev/null 2>&1; then
  fail "a missing known_hosts was accepted"
fi
assert_equal "" "$(cat "$test_root/ssh.log")" "a network command ran without pinned host keys"

reset_logs
if KEY_OVERRIDE="$test_root/missing_key" run_offhost "$set_id" >/dev/null 2>&1; then
  fail "a missing SSH key was accepted"
fi
assert_equal "" "$(cat "$test_root/ssh.log")" "a network command ran without a key"

if [ "$chmod_supported" = "true" ]; then
  reset_logs
  loose_key="$test_root/loose_key"
  cp "$ssh_key" "$loose_key"
  chmod 644 "$loose_key"
  if KEY_OVERRIDE="$loose_key" run_offhost "$set_id" >/dev/null 2>&1; then
    fail "a world-readable SSH key was accepted"
  fi
  assert_equal "" "$(cat "$test_root/ssh.log")" "a network command ran with a readable key"
else
  printf 'backup-offhost test: SKIP key permission refusal (filesystem ignores chmod)\n'
fi

for unsafe in "../etc" "2026-09-03T010000Z; rm -rf /" "" "not-a-timestamp"; do
  reset_logs
  if run_offhost "$unsafe" >/dev/null 2>&1; then
    fail "unsafe set id accepted: $unsafe"
  fi
  assert_equal "" "$(cat "$test_root/ssh.log")" "a network command ran for an unsafe set id"
done

reset_logs
if HOST_OVERRIDE='backup.example.invalid; rm -rf /' run_offhost "$set_id" >/dev/null 2>&1; then
  fail "an unsafe remote host was accepted"
fi
assert_equal "" "$(cat "$test_root/ssh.log")" "a network command ran for an unsafe host"

# An unverified set must never leave the machine.
reset_logs
write_report false
if run_offhost "$set_id" >/dev/null 2>&1; then
  fail "an unverified set was copied off-host"
fi
assert_equal "" "$(cat "$test_root/ssh.log")" "an unverified set reached the network"


# --- ordering: the audit row must exist before the report claims success -------------------------
rm -rf "$remote_root/finspace"
write_report true
reset_logs
run_offhost "$set_id" >/dev/null || fail "a valid off-host copy failed"

# Captured by the fake docker at the moment the audit ran.
assert_equal "false" "$(cat "$test_root/audit-saw-report")" \
  "the report claimed off-host success before the audit row existed"
assert_equal "yes" "$(cat "$test_root/audit-saw-final")" \
  "the audit ran before the remote set was published"
assert_contains "$(cat "$set_dir/backup-set-report.json")" '"offhost_verified": true' \
  "the report was not updated after a successful audit"

# --- H: audit failure after a valid remote publish ----------------------------------------------
rm -rf "$remote_root/finspace"
write_report true
reset_logs
: >"$test_root/audit.fail"
if run_offhost "$set_id" >/dev/null 2>&1; then
  fail "a failed audit exited zero"
fi
rm "$test_root/audit.fail"

# The remote data is genuinely published and correct, so it is deliberately kept.
[ -d "$final" ] || fail "a valid remote set was destroyed because the audit failed"
assert_equal "$dump_sha" "$(sha256sum "$final/database.dump" | awk '{print $1}')" \
  "the published remote set was damaged"
[ ! -d "$remote_root/finspace/sets/.$set_id.partial" ] || fail "a partial survived an audit failure"

# But nothing may claim off-host success without the audit row Stage B relies on.
audit_report=$(cat "$set_dir/backup-set-report.json")
assert_contains "$audit_report" '"offhost_verified": false' \
  "the report claimed off-host success without an audit row"
assert_contains "$audit_report" '"offhost_verified_at": null' "a success timestamp was recorded"
assert_contains "$audit_report" '"offhost_destination_label": null' "a destination label was recorded"
assert_contains "$audit_report" '"error": "remote set published but audit recording failed"' \
  "the audit failure left no safe evidence"
assert_contains "$audit_report" '"local_verified": true' "local verification was lost"
case "$audit_report" in
  *"$remote_root"*|*"$ssh_key"*|*"backup.example.invalid"*|*"finspace-backup"*)
    fail "the failure evidence leaked a path, host, user or key" ;;
esac

# --- B (extended): a failed transfer records evidence and leaves nothing behind -------------------
rm -rf "$remote_root/finspace"
write_report true
reset_logs
: >"$test_root/rsync.fail"
if run_offhost "$set_id" >/dev/null 2>&1; then
  fail "a failed transfer exited zero"
fi
rm "$test_root/rsync.fail"

[ ! -d "$remote_root/finspace/sets/$set_id" ] || fail "a failed transfer published a final set"
[ ! -d "$remote_root/finspace/sets/.$set_id.partial" ] || fail "the remote partial was not discarded"
assert_equal "0" "$(grep -c 'backup-remote-audit.sh' "$test_root/docker.log" || true)" \
  "an audit row was recorded for a failed transfer"
rsync_report=$(cat "$set_dir/backup-set-report.json")
assert_contains "$rsync_report" '"offhost_verified": false' "a failed transfer reported success"
assert_contains "$rsync_report" '"error": "remote transfer failed"' \
  "a failed transfer left no safe evidence"
case "$rsync_report" in
  *"$remote_root"*|*"$ssh_key"*|*"backup.example.invalid"*|*"finspace-backup"*)
    fail "the failure evidence leaked a path, host, user or key" ;;
esac
# Local artifacts are untouched.
assert_equal "$dump_sha" "$(sha256sum "$dump_path" | awk '{print $1}')" "local dump after failure"
[ -s "$dump_path.manifest.json" ] || fail "the local database manifest was disturbed"
[ -s "$set_dir/backup-set.json" ] || fail "the local inventory was disturbed"

printf 'backup-offhost test: PASS\n'
