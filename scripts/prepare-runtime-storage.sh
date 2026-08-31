#!/bin/sh
set -eu

umask 027

fail() {
  printf 'runtime storage preparation: FAIL: %s\n' "$1" >&2
  exit 1
}

read_numeric_setting() {
  setting_name=$1
  setting_file=$2
  setting_line=$(grep "^${setting_name}=" "$setting_file" || true)
  setting_value=${setting_line#*=}

  if [ "$setting_value" = "$setting_line" ] || [ -z "$setting_value" ]; then
    fail "${setting_name} is missing from backend/runtime-identity.env"
  fi
  case "$setting_value" in
    *[!0-9]*) fail "${setting_name} must be one decimal integer" ;;
  esac
  printf '%s\n' "$setting_value"
}

reject_symlink_components() {
  relative_path=$1
  current_path=$project_root
  remaining_path=$relative_path

  while [ -n "$remaining_path" ]; do
    component=${remaining_path%%/*}
    if [ "$component" = "$remaining_path" ]; then
      remaining_path=
    else
      remaining_path=${remaining_path#*/}
    fi
    current_path="$current_path/$component"
    if [ -L "$current_path" ]; then
      fail "refusing symlink in approved runtime path: $relative_path"
    fi
  done
}

prepare_directory() {
  relative_path=$1
  target_path="$project_root/$relative_path"
  parent_path=${target_path%/*}

  reject_symlink_components "$relative_path"
  if [ ! -e "$parent_path" ]; then
    mkdir -p "$parent_path"
    # Shared parents are not part of the ownership contract. If a fresh checkout
    # somehow lacks one, allow traversal without granting list or write access.
    chmod 0711 "$parent_path"
  fi
  mkdir -p "$target_path"
  [ -d "$target_path" ] || fail "runtime path is not a directory: $relative_path"

  # Deliberately not recursive: existing files and sibling data are outside this
  # ownership initialization contract.
  chown "$repository_uid:$runtime_gid" "$target_path"
  chmod 2770 "$target_path"

  actual_uid=$(stat -c '%u' "$target_path")
  actual_gid=$(stat -c '%g' "$target_path")
  actual_mode=$(stat -c '%a' "$target_path")
  if [ "$actual_uid:$actual_gid:$actual_mode" != "$repository_uid:$runtime_gid:2770" ]; then
    fail "runtime path verification failed: $relative_path"
  fi
  if ! setpriv --reuid="$runtime_uid" --regid="$runtime_gid" --clear-groups \
    test -w "$target_path"; then
    fail "runtime identity cannot write path: $relative_path"
  fi
  printf 'runtime storage prepared: %s uid=%s gid=%s mode=%s\n' \
    "$relative_path" "$actual_uid" "$actual_gid" "$actual_mode"
}

if [ "$#" -gt 1 ]; then
  fail "usage: $0 [absolute-project-root]"
fi
if [ "$(id -u)" -ne 0 ]; then
  fail "run this command as root"
fi
command -v setpriv >/dev/null 2>&1 || fail "setpriv is required to verify runtime access"

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
default_root=$(CDPATH= cd -- "$script_directory/.." && pwd -P)
requested_root=${1:-$default_root}
[ -d "$requested_root" ] || fail "project root does not exist: $requested_root"
project_root=$(CDPATH= cd -- "$requested_root" && pwd -P)
case "$project_root" in
  ""|/) fail "refusing unsafe project root" ;;
esac

for marker in docker-compose.yml compose.production.yml backend/runtime-identity.env; do
  [ -f "$project_root/$marker" ] || fail "project root marker is missing: $marker"
done

identity_file="$project_root/backend/runtime-identity.env"
runtime_uid=$(read_numeric_setting FINSPACE_RUNTIME_UID "$identity_file")
runtime_gid=$(read_numeric_setting FINSPACE_RUNTIME_GID "$identity_file")
[ "$runtime_uid" -gt 0 ] || fail "FINSPACE_RUNTIME_UID must not be root"
[ "$runtime_gid" -gt 0 ] || fail "FINSPACE_RUNTIME_GID must not be root"
repository_uid=$(stat -c '%u' "$project_root")

prepare_directory data/imports
prepare_directory data/acceptance
prepare_directory backups/acceptance-reports

printf 'runtime storage preparation: PASS\n'
