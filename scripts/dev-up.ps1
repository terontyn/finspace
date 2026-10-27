$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location $ProjectRoot
try {
    if (-not (Test-Path -LiteralPath ".env")) {
        $Answer = Read-Host ".env is missing. Copy .env.example to .env now? [y/N]"
        if ($Answer -notmatch "^(y|yes)$") {
            throw "Create .env before starting: Copy-Item .env.example .env"
        }
        Copy-Item -LiteralPath ".env.example" -Destination ".env"
        Write-Host "Created .env. Review POSTGRES_PASSWORD before non-local use."
    }

    docker compose up -d --build
    docker compose ps
    Write-Host "Frontend: http://localhost:3000"
    Write-Host "API:      http://localhost:8000"
    Write-Host "Swagger:  http://localhost:8000/docs"
    Write-Host "Adminer:  http://localhost:8080"
    Write-Host "Next:     docker compose exec backend alembic upgrade head"
} finally {
    Pop-Location
}
