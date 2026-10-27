#!/bin/sh
# The production Compose wrapper is a host dependency: every production command in the runbooks
# goes through it. What matters is not that it "works" but that it is boring and exact — always
# the same two files in the same order, always the same project directory, arguments untouched,
# and the caller's exit code that of docker compose rather than of a shell in between.
set -eu

fail() {
  printf 'finspace-compose test: FAIL: %s\n' "$1" >&2
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
wrapper="$repository_root/scripts/finspace-compose.sh"
installer="$repository_root/scripts/install-finspace-compose.sh"

test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT HUP INT TERM

bin="$test_root/bin"
project="$test_root/opt/finspace"
mkdir -p "$bin" "$project"
: >"$project/docker-compose.yml"
: >"$project/compose.production.yml"

# A fake docker records its argv one entry per line, so an argument containing a space is
# distinguishable from two arguments — the property a wrapper is most likely to get wrong.
cat >"$bin/docker" <<'STUB'
#!/bin/sh
: >"$DOCKER_ARGV_LOG"
for argument in "$@"; do
  printf '%s\n' "$argument" >>"$DOCKER_ARGV_LOG"
done
exit "${DOCKER_EXIT_CODE:-0}"
STUB
chmod 755 "$bin/docker"

argv_log="$test_root/docker-argv.log"

# The fake docker's exit code travels through this variable rather than through a command prefix:
# a prefixed assignment on a function call persists after the call in some POSIX shells and would
# leak into the next case.
docker_exit_code=0

# Both scripts are run the way an operator runs them — directly, through the shebang — never as
# `sh <script>`. Interposing an interpreter supplies the execute permission the file is supposed to
# carry itself, which is exactly how a checkout that ships them non-executable slipped through.
run_wrapper() {
  DOCKER_ARGV_LOG="$argv_log" \
  DOCKER_EXIT_CODE="$docker_exit_code" \
  PATH="$bin:/usr/bin:/bin" \
  FINSPACE_PROJECT_ROOT="$project" \
  "$wrapper" "$@"
}

# --- both scripts are executable in the checkout -------------------------------------------------
# `sudo ./scripts/install-finspace-compose.sh` is the documented command. A file committed with
# mode 100644 fails it with "Permission denied" on a fresh clone, and no amount of documentation
# repairs that: the permission has to survive the checkout.
[ -x "$installer" ] || fail "scripts/install-finspace-compose.sh is not executable in the checkout"
[ -x "$wrapper" ] || fail "scripts/finspace-compose.sh is not executable in the checkout"

# The runtime bit above is the property that matters, but on some filesystems (Windows checkouts,
# bind mounts that flatten permissions) it is reported the same either way. Where Git can answer
# for this working tree, ask it what will actually be checked out instead.
if command -v git >/dev/null 2>&1 &&
  git -C "$repository_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  for tracked in scripts/finspace-compose.sh scripts/install-finspace-compose.sh \
    scripts/tests/finspace-compose.test.sh; do
    index_mode=$(git -C "$repository_root" ls-files -s -- "$tracked" | cut -d' ' -f1)
    assert_equal "100755" "$index_mode" "index mode of $tracked"
  done
else
  printf 'finspace-compose test: note: Git index modes not checked here\n'
fi

# --- no generated code ------------------------------------------------------------------------
# A wrapper that builds its command line as a string and evals it can be steered by an argument.
for script in "$wrapper" "$installer"; do
  # Comments are stripped first: both scripts document that they avoid eval, and the promise
  # itself must not be what satisfies the check.
  if grep -v '^[[:space:]]*#' "$script" | grep -q 'eval'; then
    fail "$(basename "$script") uses eval"
  fi
done

# --- the exact command ------------------------------------------------------------------------
: >"$argv_log"
run_wrapper ps || fail "a normal invocation failed"

cat >"$test_root/expected-argv" <<EOF
compose
--project-directory
$project
--file
$project/docker-compose.yml
--file
$project/compose.production.yml
ps
EOF
if ! cmp -s "$test_root/expected-argv" "$argv_log"; then
  printf 'expected:\n' >&2
  cat "$test_root/expected-argv" >&2
  printf 'actual:\n' >&2
  cat "$argv_log" >&2
  fail "the wrapper did not invoke docker compose with the expected argument vector"
fi

# The overlay must come second: Compose applies later files on top of earlier ones, so a swapped
# order silently returns the development topology.
base_line=$(grep -n "^$project/docker-compose.yml\$" "$argv_log" | cut -d: -f1)
overlay_line=$(grep -n "^$project/compose.production.yml\$" "$argv_log" | cut -d: -f1)
[ "$base_line" -lt "$overlay_line" ] || fail "the production overlay is not applied after the base file"

# --- arguments pass through verbatim ------------------------------------------------------------
: >"$argv_log"
run_wrapper logs --tail 100 backend || fail "a multi-argument invocation failed"
assert_equal "11" "$(wc -l <"$argv_log" | tr -d ' ')" "argument count for a three-argument command"
assert_equal "backend" "$(tail -n 1 "$argv_log")" "the last user argument"

: >"$argv_log"
run_wrapper run --rm backup sh -c 'echo one two' || fail "an invocation with a quoted argument failed"
# One argument containing spaces must arrive as one argument, not three.
assert_equal "1" "$(grep -c '^echo one two$' "$argv_log")" "a quoted argument was split"
# A glob character must not be expanded on the way through.
: >"$argv_log"
run_wrapper logs 'back*' || fail "an invocation with a glob argument failed"
assert_equal "1" "$(grep -c '^back\*$' "$argv_log")" "a glob argument was expanded"

# --- the exit code is docker compose's --------------------------------------------------------
docker_exit_code=7
status=0
run_wrapper up -d || status=$?
assert_equal "7" "$status" "the wrapper replaced docker compose's exit code"

docker_exit_code=0
status=0
run_wrapper up -d || status=$?
assert_equal "0" "$status" "a successful command did not exit zero"

# --- refusals happen before docker runs ---------------------------------------------------------
assert_wrapper_refuses() {
  label=$1
  shift
  : >"$argv_log"
  status=0
  message=$("$@" 2>&1) || status=$?
  assert_equal "2" "$status" "$label: exit code"
  [ ! -s "$argv_log" ] || fail "$label: docker was invoked anyway"
  assert_contains "$message" "finspace-compose:" "$label: no diagnostic was printed"
}

assert_wrapper_refuses "a relative project root" \
  env DOCKER_ARGV_LOG="$argv_log" PATH="$bin:/usr/bin:/bin" \
  FINSPACE_PROJECT_ROOT="relative/path" sh "$wrapper" ps

assert_wrapper_refuses "a project root that does not exist" \
  env DOCKER_ARGV_LOG="$argv_log" PATH="$bin:/usr/bin:/bin" \
  FINSPACE_PROJECT_ROOT="$test_root/absent" sh "$wrapper" ps

mkdir -p "$test_root/incomplete"
: >"$test_root/incomplete/docker-compose.yml"
assert_wrapper_refuses "a checkout without the production overlay" \
  env DOCKER_ARGV_LOG="$argv_log" PATH="$bin:/usr/bin:/bin" \
  FINSPACE_PROJECT_ROOT="$test_root/incomplete" sh "$wrapper" ps

mkdir -p "$test_root/no-base"
: >"$test_root/no-base/compose.production.yml"
assert_wrapper_refuses "a checkout without the base compose file" \
  env DOCKER_ARGV_LOG="$argv_log" PATH="$bin:/usr/bin:/bin" \
  FINSPACE_PROJECT_ROOT="$test_root/no-base" sh "$wrapper" ps

mkdir -p "$test_root/empty-bin"
assert_wrapper_refuses "a host without docker" \
  env DOCKER_ARGV_LOG="$argv_log" PATH="$test_root/empty-bin" \
  FINSPACE_PROJECT_ROOT="$project" "$wrapper" ps

# --- the wrapper carries no secrets and no host specifics ---------------------------------------
wrapper_text=$(cat "$wrapper")
for forbidden in PASSWORD SECRET_KEY ENCRYPTION_KEY TOKEN; do
  case "$wrapper_text" in
    *"$forbidden"*) fail "the wrapper mentions $forbidden" ;;
  esac
done
assert_contains "$wrapper_text" "/opt/finspace" "the wrapper does not default to the supported project root"

# --- the installer ------------------------------------------------------------------------------
target_directory="$test_root/usr-local-bin"
mkdir -p "$target_directory"

status=0
message=$(FINSPACE_COMPOSE_BIN="relative/finspace-compose" sh "$installer" 2>&1) || status=$?
[ "$status" -ne 0 ] || fail "the installer accepted a relative target path"
assert_contains "$message" "FAIL" "the installer printed no diagnostic for a relative target"

status=0
message=$(FINSPACE_COMPOSE_BIN="$test_root/absent-directory/finspace-compose" sh "$installer" 2>&1) || status=$?
[ "$status" -ne 0 ] || fail "the installer accepted a missing target directory"

status=0
message=$(sh "$installer" extra-argument 2>&1) || status=$?
[ "$status" -ne 0 ] || fail "the installer accepted a positional argument"

if [ "$(id -u)" -eq 0 ]; then
  target="$target_directory/finspace-compose"
  FINSPACE_COMPOSE_BIN="$target" "$installer" >"$test_root/install.log" 2>&1 ||
    fail "the installer failed as root"
  assert_contains "$(cat "$test_root/install.log")" "PASS" "the installer printed no success line"
  cmp -s "$wrapper" "$target" || fail "the installed wrapper differs from the repository copy"
  assert_equal "755" "$(stat -c '%a' "$target")" "installed mode"
  assert_equal "0" "$(stat -c '%u' "$target")" "installed owner"

  # Re-running is how an update is performed, so it must be idempotent rather than an error.
  FINSPACE_COMPOSE_BIN="$target" "$installer" >/dev/null 2>&1 ||
    fail "re-running the installer failed"
  cmp -s "$wrapper" "$target" || fail "the update left a different wrapper in place"

  # A host still pinning the retired wrapper path must stop the deploy rather than be installed
  # over silently. The detection itself is covered by check-backup-env-wrapper.test.sh; what
  # matters here is that the installer is wired to it and propagates its exit code.
  printf 'FINSPACE_COMPOSE=/usr/local/sbin/finspace-compose\n' >"$test_root/stale-backup.env"
  status=0
  message=$(FINSPACE_COMPOSE_BIN="$target" FINSPACE_BACKUP_ENV="$test_root/stale-backup.env" \
    "$installer" 2>&1) || status=$?
  [ "$status" -eq 3 ] || fail "a stale backup.env wrapper pin did not stop the install (exit $status)"
  assert_contains "$message" "REPAIR REQUIRED" "the installer hid the backup.env diagnostic"
  cmp -s "$wrapper" "$target" || fail "the wrapper was not installed before the host check ran"

  printf 'FINSPACE_COMPOSE=finspace-compose\n' >"$test_root/good-backup.env"
  FINSPACE_COMPOSE_BIN="$target" FINSPACE_BACKUP_ENV="$test_root/good-backup.env" \
    "$installer" >/dev/null 2>&1 || fail "a correct backup.env still failed the install"

  # The installed copy is what production actually runs.
  : >"$argv_log"
  DOCKER_ARGV_LOG="$argv_log" PATH="$bin:/usr/bin:/bin" \
    FINSPACE_PROJECT_ROOT="$project" "$target" ps || fail "the installed wrapper failed to run"
  cmp -s "$test_root/expected-argv" "$argv_log" ||
    fail "the installed wrapper built a different command than the repository copy"
else
  status=0
  message=$(FINSPACE_COMPOSE_BIN="$target_directory/finspace-compose" "$installer" 2>&1) || status=$?
  [ "$status" -ne 0 ] || fail "the installer ran without root"
  assert_contains "$message" "root" "the installer did not explain that root is required"
  printf 'finspace-compose test: note: not root, install assertions were reduced\n'
fi

printf 'finspace-compose test: PASS\n'
