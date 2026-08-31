#!/bin/sh
set -eu

umask 077

fail() {
  printf 'strict git status: FAIL: %s\n' "$1" >&2
  exit 1
}

[ "$#" -eq 0 ] || fail "usage: $0"
command -v git >/dev/null 2>&1 || fail "git is required"

diagnostics_file=$(mktemp "${TMPDIR:-/tmp}/finspace-git-status.XXXXXX")
cleanup() {
  rm -f -- "$diagnostics_file"
}
trap cleanup EXIT HUP INT TERM

if ! status_output=$(git status --short 2>"$diagnostics_file"); then
  cat "$diagnostics_file" >&2
  fail "git status command failed"
fi
if [ -s "$diagnostics_file" ]; then
  cat "$diagnostics_file" >&2
  fail "git status emitted diagnostics"
fi

if [ -n "$status_output" ]; then
  printf '%s\n' "$status_output"
fi
