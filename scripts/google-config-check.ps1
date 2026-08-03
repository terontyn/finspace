$ErrorActionPreference = "Stop"
docker compose exec backend python scripts/google_config_check.py
if ($LASTEXITCODE -ne 0) {
    throw "Google configuration is incomplete. Secret values were not printed."
}
