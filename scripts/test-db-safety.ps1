$ErrorActionPreference = "Stop"

docker compose exec -e TESTING=true backend python scripts/test_db_safety_check.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

docker compose exec -e TESTING=true backend python scripts/test_runner.py tests/test_test_database_safety.py -q
exit $LASTEXITCODE
