#!/bin/sh
set -eu

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -f .env ]; then
  printf '.env is missing. Copy .env.example to .env now? [y/N] '
  read -r answer
  case "$answer" in
    y|Y|yes|YES)
      cp .env.example .env
      echo "Created .env. Review POSTGRES_PASSWORD before non-local use."
      ;;
    *)
      echo "Create .env before starting: cp .env.example .env" >&2
      exit 1
      ;;
  esac
fi

docker compose up -d --build
docker compose ps
echo "Frontend: http://localhost:3000"
echo "API:      http://localhost:8000"
echo "Swagger:  http://localhost:8000/docs"
echo "Adminer:  http://localhost:8080"
echo "Next:     docker compose exec backend alembic upgrade head"
