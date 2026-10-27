#!/bin/sh
# The n8n archive is optional and COLD. These tests prove the orchestration around the archive:
# only n8n is stopped, its prior state is restored whatever happens, and no credential export is
# ever invoked.
set -eu

fail() {
  printf 'n8n-archive test: FAIL: %s\n' "$1" >&2
  exit 1
}

assert_equal() {
  [ "$2" = "$1" ] || fail "$3: expected [$1], got [$2]"
}

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

bin="$test_root/bin"
mkdir -p "$bin"

# A fake `docker` that records every invocation and answers `ps` from a state file.
cat >"$bin/docker" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$DOCKER_LOG"
for argument in "$@"; do
  case "$argument" in
    ps)
      [ "$(cat "$N8N_STATE")" = "running" ] && printf 'n8n\n'
      exit 0
      ;;
    stop)
      printf 'stopped' >"$N8N_STATE"
      exit 0
      ;;
    start)
      printf 'running' >"$N8N_STATE"
      exit 0
      ;;
    run)
      [ ! -f "$ARCHIVE_FAIL" ] || exit 1
      printf 'archived\n'
      exit 0
      ;;
  esac
done
exit 0
STUB
chmod 755 "$bin/docker"

run_archive() {
  PATH="$bin:/usr/bin:/bin" \
  DOCKER_LOG="$test_root/docker.log" \
  N8N_STATE="$test_root/n8n.state" \
  ARCHIVE_FAIL="$test_root/archive.fail" \
  sh "$repository_root/scripts/n8n-archive.sh" "$@"
}

set_id="2026-09-03T010000Z"

# --- n8n was running: stop, archive, restart --------------------------------------------------
: >"$test_root/docker.log"
printf 'running' >"$test_root/n8n.state"
run_archive "$set_id" >/dev/null || fail "archiving a running n8n failed"
assert_equal "running" "$(cat "$test_root/n8n.state")" "n8n state after a successful archive"
log=$(cat "$test_root/docker.log")
case "$log" in *"compose stop n8n"*) ;; *) fail "n8n was not stopped before the archive" ;; esac
case "$log" in *"compose start n8n"*) ;; *) fail "n8n was not restarted" ;; esac
case "$log" in *"n8n-archive-volume.sh $set_id"*) ;; *) fail "the volume archive was not invoked" ;; esac
case "$log" in *"--profile tools"*) ;; *) fail "the helper must run under the tools profile" ;; esac

# Ordering: the stop must precede the archive, and the restart must follow it.
stop_line=$(grep -n "compose stop n8n" "$test_root/docker.log" | head -n 1 | cut -d: -f1)
run_line=$(grep -n "n8n-archive-volume.sh" "$test_root/docker.log" | head -n 1 | cut -d: -f1)
start_line=$(grep -n "compose start n8n" "$test_root/docker.log" | head -n 1 | cut -d: -f1)
[ "$stop_line" -lt "$run_line" ] || fail "the archive ran before n8n was stopped"
[ "$run_line" -lt "$start_line" ] || fail "n8n restarted before the archive completed"

# Only n8n may be touched.
for service in postgres redis backend frontend sync-worker categorization-prune; do
  case "$log" in
    *"stop $service"*) fail "$service was stopped by the n8n archive" ;;
  esac
done
# Credentials are never exported in plaintext.
case "$log" in
  *"export:credentials"*) fail "a credential export was invoked" ;;
esac

# --- n8n was already stopped: it must stay stopped ---------------------------------------------
: >"$test_root/docker.log"
printf 'stopped' >"$test_root/n8n.state"
run_archive "$set_id" >/dev/null || fail "archiving a stopped n8n failed"
assert_equal "stopped" "$(cat "$test_root/n8n.state")" "a stopped n8n must stay stopped"
case "$(cat "$test_root/docker.log")" in
  *"compose start n8n"*) fail "a previously stopped n8n was started" ;;
esac

# --- archive failure must restore the previous running state -----------------------------------
: >"$test_root/docker.log"
printf 'running' >"$test_root/n8n.state"
: >"$test_root/archive.fail"
if run_archive "$set_id" >/dev/null 2>&1; then
  fail "a failed archive exited zero"
fi
assert_equal "running" "$(cat "$test_root/n8n.state")" "n8n state after a failed archive"
rm "$test_root/archive.fail"

# --- unsafe set ids are refused before anything is stopped -------------------------------------
for unsafe in "../etc" "2026-09-03T010000Z; rm -rf /" "" "not-a-timestamp"; do
  : >"$test_root/docker.log"
  printf 'running' >"$test_root/n8n.state"
  if run_archive "$unsafe" >/dev/null 2>&1; then
    fail "unsafe set id accepted: $unsafe"
  fi
  case "$(cat "$test_root/docker.log")" in
    *"compose stop n8n"*) fail "n8n was stopped before the set id was validated" ;;
  esac
done

# --- the in-container archiver produces a hashed, atomically published artifact ------------------
volume_root="$test_root/volume-sandbox"
mkdir -p "$volume_root/source/.n8n-state" "$volume_root/backups"
printf 'encrypted-credential-blob' >"$volume_root/source/database.sqlite"
sed -e "s#/backups#$volume_root/backups#g" \
  "$repository_root/scripts/n8n-archive-volume.sh" >"$volume_root/archive.sh"
N8N_SOURCE_DIR="$volume_root/source" sh "$volume_root/archive.sh" "$set_id" >/dev/null ||
  fail "the volume archiver failed"
archive="$volume_root/backups/sets/$set_id/n8n-data.tar.gz"
digest="$volume_root/backups/sets/$set_id/n8n-data.sha256"
[ -s "$archive" ] || fail "no archive was produced"
[ -s "$digest" ] || fail "no digest was produced"
assert_equal "$(sha256sum "$archive" | awk '{print $1}')" "$(cat "$digest")" "recorded digest"
tar --list --file="$archive" >/dev/null || fail "the archive is not readable"
tar --list --file="$archive" | grep -q "database.sqlite" || fail "the volume content is missing"
if find "$volume_root/backups" -name '*.partial' | grep -q .; then
  fail "a .partial artifact survived"
fi
# Re-running must refuse rather than silently replace a published archive.
if N8N_SOURCE_DIR="$volume_root/source" sh "$volume_root/archive.sh" "$set_id" >/dev/null 2>&1; then
  fail "an existing archive was overwritten"
fi

printf 'n8n-archive test: PASS\n'
