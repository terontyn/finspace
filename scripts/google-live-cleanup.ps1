param(
    [Parameter(Mandatory = $true)][Guid]$AcceptanceRunId,
    [switch]$Confirm
)
$ErrorActionPreference = "Stop"
$arguments = @("compose", "exec", "backend", "python", "scripts/google_live_acceptance.py", "cleanup", "--run-id", $AcceptanceRunId.ToString())
if ($Confirm) { $arguments += "--yes" }
docker @arguments
if ($LASTEXITCODE -ne 0) { throw "Acceptance cleanup refused or failed." }
