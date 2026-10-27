#!/bin/sh
set -eu

if [ -z "${POSTGRES_TEST_DB:-}" ] || [ "$POSTGRES_TEST_DB" = "$POSTGRES_DB" ]; then
  exit 0
fi

database_exists="$(
  psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align \
    --set=test_database="$POSTGRES_TEST_DB" <<'SQL'
SELECT 1 FROM pg_database WHERE datname = :'test_database';
SQL
)"

if [ "$database_exists" != "1" ]; then
  createdb --username "$POSTGRES_USER" "$POSTGRES_TEST_DB"
fi
