$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Write-Warning "This removes all local Finspace containers, PostgreSQL data, Redis data, and frontend caches."
$Confirmation = Read-Host "Type RESET to continue"
if ($Confirmation -cne "RESET") {
    Write-Host "Reset cancelled. No data was removed."
    exit 0
}

Push-Location $ProjectRoot
try {
    docker compose down --volumes --remove-orphans
    Write-Host "Local Finspace containers and named volumes were removed."
} finally {
    Pop-Location
}
