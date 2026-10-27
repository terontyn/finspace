[CmdletBinding()]
param([switch]$Create)

$ErrorActionPreference = "Stop"
$arguments = @("compose", "--profile", "tools", "run", "--rm", "backup", "sh", "/scripts/verify-backup.sh")
if ($Create) { $arguments += "--create" }
Write-Host "Verifying the backup by restoring it into a temporary database..."
& docker @arguments
if ($LASTEXITCODE -ne 0) { throw "Backup verification failed with exit code $LASTEXITCODE." }
