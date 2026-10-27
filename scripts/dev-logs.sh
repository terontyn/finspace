#!/bin/sh
set -eu

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ "$#" -eq 0 ]; then
  docker compose logs --follow
elif [ "$#" -eq 1 ]; then
  case "$1" in
    postgres|redis|backend|frontend|adminer)
      docker compose logs --follow "$1"
      ;;
    *)
      echo "Unknown service: $1" >&2
      exit 2
      ;;
  esac
else
  echo "Usage: $0 [postgres|redis|backend|frontend|adminer]" >&2
  exit 2
fi
