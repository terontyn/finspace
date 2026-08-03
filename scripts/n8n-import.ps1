$ErrorActionPreference = "Stop"

docker compose exec -T n8n n8n import:workflow --separate --input=/workflows
if ($LASTEXITCODE -ne 0) {
    throw "n8n workflow import failed"
}
Write-Host "Workflow импортированы из n8n/workflows. Проверьте credentials перед активацией."
