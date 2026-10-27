param(
    [Parameter(Mandatory = $true)][string]$WorkspaceId,
    [string]$WeekStart = (Get-Date).AddDays(-(([int](Get-Date).DayOfWeek + 6) % 7)).ToString("yyyy-MM-dd"),
    [string]$BackendUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Stop"
$serviceKey = Read-Host "Введите ServiceKey n8n" -AsSecureString
$plainKey = [System.Net.NetworkCredential]::new("", $serviceKey).Password
$headers = @{
    Authorization = "ServiceKey $plainKey"
    "X-Idempotency-Key" = "weekly-report-test:${WorkspaceId}:${WeekStart}:$([guid]::NewGuid())"
}
$body = @{ workspace_id = $WorkspaceId; week_start = $WeekStart } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$BackendUrl/api/v1/automation/reports/weekly" -Headers $headers -ContentType "application/json" -Body $body
$plainKey = $null
