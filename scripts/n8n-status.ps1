$ErrorActionPreference = "Stop"

$health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:5678/healthz" -TimeoutSec 5
if ($health.StatusCode -ne 200) {
    throw "n8n healthcheck returned HTTP $($health.StatusCode)"
}

docker compose ps n8n
Write-Host "n8n доступен: http://localhost:5678"
