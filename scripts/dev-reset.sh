#!/bin/sh
set -eu

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
echo "WARNING: this removes local PostgreSQL and Redis data plus frontend caches."
printf 'Type RESET to continue: '
read -r confirmation

if [ "$confirmation" != "RESET" ]; then
  echo "Reset cancelled. No data was removed."
  exit 0
fi

cd "$PROJECT_ROOT"
docker compose down --volumes --remove-orphans
echo "Local Finspace containers and named volumes were removed."
