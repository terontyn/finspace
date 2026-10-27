param(
    [Parameter(Mandatory = $true)][string]$AccessToken,
    [Parameter(Mandatory = $true)][string]$WorkspaceId,
    [switch]$Force,
    [string]$ApiUrl = "http://localhost:8000"
)
$headers = @{ Authorization = "Bearer $AccessToken"; "X-Workspace-ID" = $WorkspaceId }
$preview = Invoke-RestMethod -Uri "$ApiUrl/api/v1/google-sheets/full-export-preview" -Headers $headers
$preview | Format-List
$confirmation = Read-Host "Type EXPORT to continue"
if ($confirmation -ne "EXPORT") { throw "Full export cancelled." }
$body = @{ force = [bool]($Force -or $preview.blocked) } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$ApiUrl/api/v1/google-sheets/full-export" -Headers $headers -ContentType "application/json" -Body $body
