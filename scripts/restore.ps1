[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DumpFile,
    [string]$TargetDatabase = "finspace_restore_test",
    [switch]$OverwriteMain
)

$ErrorActionPreference = "Stop"
$resolved = (Resolve-Path -LiteralPath $DumpFile).Path
$backupRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\backups")).Path
if (-not $resolved.StartsWith("$backupRoot$([IO.Path]::DirectorySeparatorChar)", [StringComparison]::OrdinalIgnoreCase)) {
    throw "The dump file must be inside the repository backups directory."
}
$relative = $resolved.Substring($backupRoot.Length).TrimStart('\').Replace('\', '/')
$containerPath = "/backups/$relative"
$arguments = @("compose", "--profile", "tools", "run", "--rm", "backup", "sh", "/scripts/restore.sh", $containerPath, $TargetDatabase)
if ($OverwriteMain) { $arguments += "--overwrite-main" }
Write-Host "Restoring exact file '$resolved' into '$TargetDatabase'..."
& docker @arguments
if ($LASTEXITCODE -ne 0) { throw "Restore failed with exit code $LASTEXITCODE." }
