$ErrorActionPreference = "Stop"
docker compose exec backend python scripts/google_live_acceptance.py start
if ($LASTEXITCODE -ne 0) { throw "Google live acceptance wizard could not start." }
