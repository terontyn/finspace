param([Parameter(Mandatory = $true)][Guid]$AcceptanceRunId)
$ErrorActionPreference = "Stop"
docker compose exec backend python scripts/google_live_acceptance.py report --run-id $AcceptanceRunId.ToString()
if ($LASTEXITCODE -ne 0) { throw "Acceptance report generation failed." }
