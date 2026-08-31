#!/bin/sh
set -eu

fail() {
  printf 'prepare-runtime-storage test: FAIL: %s\n' "$1" >&2
  exit 1
}

assert_equal() {
  expected=$1
  actual=$2
  label=$3
  [ "$actual" = "$expected" ] || fail "$label: expected $expected, got $actual"
}

copy_fixture_markers() {
  fixture_root=$1
  mkdir -p "$fixture_root/backend"
  chmod 0755 "$fixture_root"
  cp "$repository_root/docker-compose.yml" "$fixture_root/docker-compose.yml"
  cp "$repository_root/compose.production.yml" "$fixture_root/compose.production.yml"
  cp "$repository_root/backend/runtime-identity.env" "$fixture_root/backend/runtime-identity.env"
}

assert_runtime_directory() {
  relative_path=$1
  target_path="$fixture/$relative_path"
  [ -d "$target_path" ] || fail "missing runtime directory: $relative_path"
  assert_equal "$expected_uid" "$(stat -c '%u' "$target_path")" "$relative_path uid"
  assert_equal "$expected_gid" "$(stat -c '%g' "$target_path")" "$relative_path gid"
  assert_equal 750 "$(stat -c '%a' "$target_path")" "$relative_path mode"
  setpriv --reuid="$expected_uid" --regid="$expected_gid" --clear-groups \
    sh -c 'probe=$1/.runtime-write-test; : > "$probe"; rm -f "$probe"' sh "$target_path" \
    || fail "runtime identity cannot create files in $relative_path"
}

[ "$(id -u)" -eq 0 ] || fail "this integration test must run as root"

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "$script_directory/../.." && pwd -P)
test_parent=${TMPDIR:-/tmp}
test_root=$(mktemp -d "$test_parent/finspace-storage-test.XXXXXX")
chmod 0755 "$test_root"
cleanup() {
  case "$test_root" in
    "$test_parent"/finspace-storage-test.*) rm -rf -- "$test_root" ;;
    *) printf 'prepare-runtime-storage test: refusing unsafe cleanup path\n' >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

expected_uid=$(sed -n 's/^FINSPACE_RUNTIME_UID=//p' "$repository_root/backend/runtime-identity.env")
expected_gid=$(sed -n 's/^FINSPACE_RUNTIME_GID=//p' "$repository_root/backend/runtime-identity.env")
fixture="$test_root/project"
copy_fixture_markers "$fixture"

if setpriv --reuid="$expected_uid" --regid="$expected_gid" --clear-groups \
  "$repository_root/scripts/prepare-runtime-storage.sh" "$fixture" >/dev/null 2>&1; then
  fail "non-root invocation was accepted"
fi

mkdir -p "$fixture/unrelated"
printf 'keep-me\n' > "$fixture/unrelated/sentinel"
chmod 0711 "$fixture/unrelated"
unrelated_before=$(stat -c '%u:%g:%a' "$fixture/unrelated")
unrelated_hash_before=$(sha256sum "$fixture/unrelated/sentinel")

"$repository_root/scripts/prepare-runtime-storage.sh" "$fixture"
assert_runtime_directory data/imports
assert_runtime_directory data/acceptance
assert_runtime_directory backups/acceptance-reports

# A second run must converge without recursively changing existing runtime data.
printf 'existing-data\n' > "$fixture/data/imports/existing"
chown 123:124 "$fixture/data/imports/existing"
chmod 0600 "$fixture/data/imports/existing"
existing_before=$(stat -c '%u:%g:%a' "$fixture/data/imports/existing")
first_directory_state=$(stat -c '%u:%g:%a' "$fixture/data/imports")

"$repository_root/scripts/prepare-runtime-storage.sh" "$fixture"
assert_runtime_directory data/imports
assert_runtime_directory data/acceptance
assert_runtime_directory backups/acceptance-reports
assert_equal "$first_directory_state" "$(stat -c '%u:%g:%a' "$fixture/data/imports")" "idempotent directory state"
assert_equal "$existing_before" "$(stat -c '%u:%g:%a' "$fixture/data/imports/existing")" "existing file ownership"
assert_equal "$unrelated_before" "$(stat -c '%u:%g:%a' "$fixture/unrelated")" "unrelated directory"
assert_equal "$unrelated_hash_before" "$(sha256sum "$fixture/unrelated/sentinel")" "unrelated file"

# A symlink in an allowlisted path must fail closed without touching its target.
unsafe_fixture="$test_root/unsafe-project"
outside="$test_root/outside"
copy_fixture_markers "$unsafe_fixture"
mkdir -p "$unsafe_fixture/data" "$outside"
chmod 0700 "$outside"
outside_before=$(stat -c '%u:%g:%a' "$outside")
ln -s "$outside" "$unsafe_fixture/data/imports"
if "$repository_root/scripts/prepare-runtime-storage.sh" "$unsafe_fixture" >/dev/null 2>&1; then
  fail "symlinked runtime path was accepted"
fi
assert_equal "$outside_before" "$(stat -c '%u:%g:%a' "$outside")" "symlink target"

if "$repository_root/scripts/prepare-runtime-storage.sh" / >/dev/null 2>&1; then
  fail "filesystem root was accepted as a project root"
fi

printf 'prepare-runtime-storage test: PASS\n'
