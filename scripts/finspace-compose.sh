#!/bin/sh
# Production Compose wrapper, installed on the HOST as /usr/local/bin/finspace-compose.
#
# It exists so "production" is one fixed, reviewable command instead of an operator remembering
# two -f flags in the right order. The production overlay is read from the checkout itself, so
# selecting a release also selects the topology; there is no second copy of it anywhere on the
# host to drift out of date.
#
# Contract, relied on by scripts/tests/finspace-compose.test.sh:
#   * no eval and no generated code;
#   * fixed compose file order, base first, production overlay second;
#   * an explicit project directory, so the result does not depend on the caller's cwd;
#   * arguments passed through verbatim, including arguments containing spaces;
#   * docker compose's exit code becomes this command's exit code;
#   * no secrets: the wrapper never reads .env, it only tells Compose where to find it.
set -eu

# Wrapper misuse exits 2 so it stays distinguishable from anything docker compose reports.
fail() {
  printf 'finspace-compose: %s\n' "$1" >&2
  exit 2
}

# The default is the supported production location. FINSPACE_PROJECT_ROOT is for rehearsals and
# tests: production runs under sudo, and the production sudo policy does not preserve the caller's
# environment, so `sudo finspace-compose` always resolves to /opt/finspace.
project_root="${FINSPACE_PROJECT_ROOT:-/opt/finspace}"
case "$project_root" in
  /*) ;;
  *) fail "FINSPACE_PROJECT_ROOT must be an absolute path" ;;
esac

base_file="$project_root/docker-compose.yml"
production_file="$project_root/compose.production.yml"

[ -d "$project_root" ] || fail "project root does not exist: $project_root"
[ -f "$base_file" ] || fail "base compose file is missing: $base_file"
[ -f "$production_file" ] || fail "production overlay is missing: $production_file"
command -v docker >/dev/null 2>&1 || fail "docker is not installed or not on PATH"

# exec so the caller observes docker compose's own exit status, with no shell in between.
exec docker compose \
  --project-directory "$project_root" \
  --file "$base_file" \
  --file "$production_file" \
  "$@"
