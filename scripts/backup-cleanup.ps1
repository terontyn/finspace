[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Write-Host "Applying the local backup retention policy..."
docker compose --profile tools run --rm backup sh /scripts/backup-cleanup.sh
if ($LASTEXITCODE -ne 0) { throw "Backup cleanup failed with exit code $LASTEXITCODE." }
