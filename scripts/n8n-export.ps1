$ErrorActionPreference = "Stop"

$exportPath = Join-Path $PSScriptRoot "..\n8n\workflows\exported"
New-Item -ItemType Directory -Force -Path $exportPath | Out-Null
docker compose exec -T n8n n8n export:workflow --all --separate --output=/workflows/exported
if ($LASTEXITCODE -ne 0) {
    throw "n8n workflow export failed"
}
Write-Host "Workflow экспортированы в n8n/workflows/exported."
