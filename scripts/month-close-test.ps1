param(
    [Parameter(Mandatory = $true)][string]$AccessToken,
    [Parameter(Mandatory = $true)][string]$WorkspaceId,
    [Parameter(Mandatory = $true)][int]$Year,
    [Parameter(Mandatory = $true)][ValidateRange(1, 12)][int]$Month,
    [string]$BackendUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Stop"
$headers = @{ Authorization = "Bearer $AccessToken"; "X-Workspace-ID" = $WorkspaceId }
Invoke-RestMethod -Method Post -Uri "$BackendUrl/api/v1/month-close/$Year/$Month/prepare" -Headers $headers
