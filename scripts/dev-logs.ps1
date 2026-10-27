param(
    [Parameter(Position = 0)]
    [ValidateSet("postgres", "redis", "backend", "frontend", "adminer")]
    [string]$Service
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location $ProjectRoot
try {
    if ($Service) {
        docker compose logs --follow $Service
    } else {
        docker compose logs --follow
    }
} finally {
    Pop-Location
}
