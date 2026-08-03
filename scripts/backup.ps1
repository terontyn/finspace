[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Write-Host "Creating a PostgreSQL custom-format backup..."
docker compose --profile tools run --rm backup sh /scripts/backup.sh
if ($LASTEXITCODE -ne 0) { throw "Backup failed with exit code $LASTEXITCODE." }
