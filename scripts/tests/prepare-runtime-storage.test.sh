#!/bin/sh
set -eu

fail() {
  printf 'prepare-runtime-storage test: FAIL: %s\n' "$1" >&2
  exit 1
}

# Exit 4 means "this environment cannot run the suite", which is not the same as a defect: this
# test changes ownership and drops privileges, so it is meaningless anywhere but a Linux host with
# root. The release gate records such a suite as skipped and names it, so it stays visible.
skip() {
  printf 'prepare-runtime-storage test: SKIP: %s\n' "$1"
  exit 4
}

assert_equal() {
  expected=$1
  actual=$2
  label=$3
  [ "$actual" = "$expected" ] || fail "$label: expected $expected, got $actual"
}

copy_fixture_markers() {
  fixture_root=$1
  mkdir -p \
    "$fixture_root/backend" \
    "$fixture_root/data/imports" \
    "$fixture_root/data/acceptance" \
    "$fixture_root/backups/acceptance-reports"
  chmod 0755 "$fixture_root"
  cp "$repository_root/docker-compose.yml" "$fixture_root/docker-compose.yml"
  cp "$repository_root/compose.production.yml" "$fixture_root/compose.production.yml"
  cp "$repository_root/.gitignore" "$fixture_root/.gitignore"
  printf '\n/unrelated/\n' >>"$fixture_root/.gitignore"
  cp "$repository_root/backend/runtime-identity.env" "$fixture_root/backend/runtime-identity.env"
  cp "$repository_root/data/imports/.gitkeep" "$fixture_root/data/imports/.gitkeep"
  cp "$repository_root/data/acceptance/.gitkeep" "$fixture_root/data/acceptance/.gitkeep"
  cp \
    "$repository_root/backups/acceptance-reports/.gitkeep" \
    "$fixture_root/backups/acceptance-reports/.gitkeep"
}

initialize_fixture_repository() {
  fixture_root=$1
  git -C "$fixture_root" init -q
  git -C "$fixture_root" config user.name "Runtime storage test"
  git -C "$fixture_root" config user.email "runtime-storage-test@example.invalid"
  git -C "$fixture_root" add .
  git -C "$fixture_root" commit -qm "Initialize fixture"
  chown -R "$repository_uid:$repository_gid" "$fixture_root"
}

assert_git_status() {
  label=$1
  expected_status=$2
  stdout_file="$test_root/git-status-$label.stdout"
  stderr_file="$test_root/git-status-$label.stderr"

  if ! setpriv --reuid="$repository_uid" --regid="$repository_gid" --clear-groups \
    env HOME="$repository_home" \
    git -C "$fixture" status --porcelain >"$stdout_file" 2>"$stderr_file"; then
    cat "$stderr_file" >&2
    fail "git status failed for repository owner: $label"
  fi
  if [ -s "$stderr_file" ]; then
    cat "$stderr_file" >&2
    fail "git status emitted diagnostics for repository owner: $label"
  fi
  assert_equal "$expected_status" "$(cat "$stdout_file")" "git status: $label"

  strict_stdout_file="$test_root/git-status-strict-$label.stdout"
  strict_stderr_file="$test_root/git-status-strict-$label.stderr"
  if ! setpriv --reuid="$repository_uid" --regid="$repository_gid" --clear-groups \
    env HOME="$repository_home" \
    sh -c 'cd "$1" && exec "$2"' sh \
    "$fixture" "$repository_root/scripts/git-status-strict.sh" \
    >"$strict_stdout_file" 2>"$strict_stderr_file"; then
    cat "$strict_stderr_file" >&2
    fail "strict git status failed for repository owner: $label"
  fi
  if [ -s "$strict_stderr_file" ]; then
    cat "$strict_stderr_file" >&2
    fail "strict git status emitted diagnostics: $label"
  fi
  assert_equal \
    "$expected_status" \
    "$(cat "$strict_stdout_file")" \
    "strict git status: $label"
}

assert_runtime_directory() {
  relative_path=$1
  target_path="$fixture/$relative_path"
  [ -d "$target_path" ] || fail "missing runtime directory: $relative_path"
  assert_equal "$repository_uid" "$(stat -c '%u' "$target_path")" "$relative_path uid"
  assert_equal "$expected_gid" "$(stat -c '%g' "$target_path")" "$relative_path gid"
  assert_equal 2770 "$(stat -c '%a' "$target_path")" "$relative_path mode"
  setpriv --reuid="$expected_uid" --regid="$expected_gid" --clear-groups \
    sh -c 'probe=$1/.runtime-write-test; : > "$probe"; rm -f "$probe"' sh "$target_path" \
    || fail "runtime identity cannot create files in $relative_path"
}

[ "$(id -u)" -eq 0 ] || skip "needs root: it changes ownership and drops privileges"

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
repository_uid=$((expected_uid + 20000))
repository_gid=$((expected_gid + 20000))
repository_home="$test_root/repository-owner-home"
mkdir -p "$repository_home"
chown "$repository_uid:$repository_gid" "$repository_home"
fixture="$test_root/project"
copy_fixture_markers "$fixture"
initialize_fixture_repository "$fixture"

if setpriv --reuid="$repository_uid" --regid="$repository_gid" --clear-groups \
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
assert_git_status clean ""

setpriv --reuid="$repository_uid" --regid="$repository_gid" --clear-groups \
  sh -c 'printf "\n# detectable repository change\n" >>"$1"' sh \
  "$fixture/docker-compose.yml"
assert_git_status dirty " M docker-compose.yml"

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
assert_git_status after-second-run " M docker-compose.yml"

# The release gate must reject diagnostics even when Git itself exits zero.
fake_bin="$test_root/fake-bin"
mkdir "$fake_bin"
printf '%s\n' \
  '#!/bin/sh' \
  'printf "simulated permission warning\\n" >&2' \
  'exit 0' \
  >"$fake_bin/git"
chmod 0755 "$fake_bin/git"
if setpriv --reuid="$repository_uid" --regid="$repository_gid" --clear-groups \
  env HOME="$repository_home" PATH="$fake_bin:/usr/bin:/bin" \
  "$repository_root/scripts/git-status-strict.sh" >/dev/null 2>&1; then
  fail "strict git status accepted diagnostics from a successful Git command"
fi

# A symlink in an allowlisted path must fail closed without touching its target.
unsafe_fixture="$test_root/unsafe-project"
outside="$test_root/outside"
copy_fixture_markers "$unsafe_fixture"
mkdir -p "$unsafe_fixture/data" "$outside"
chmod 0700 "$outside"
outside_before=$(stat -c '%u:%g:%a' "$outside")
rm "$unsafe_fixture/data/imports/.gitkeep"
rmdir "$unsafe_fixture/data/imports"
ln -s "$outside" "$unsafe_fixture/data/imports"
if "$repository_root/scripts/prepare-runtime-storage.sh" "$unsafe_fixture" >/dev/null 2>&1; then
  fail "symlinked runtime path was accepted"
fi
assert_equal "$outside_before" "$(stat -c '%u:%g:%a' "$outside")" "symlink target"

if "$repository_root/scripts/prepare-runtime-storage.sh" / >/dev/null 2>&1; then
  fail "filesystem root was accepted as a project root"
fi

printf 'prepare-runtime-storage test: PASS\n'
