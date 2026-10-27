#!/bin/sh
set -eu

[ "${TESTING:-}" = "true" ] || {
  echo "TEST DATABASE SAFETY: run-tests.sh requires TESTING=true" >&2
  exit 4
}

exec python scripts/test_runner.py "$@"
