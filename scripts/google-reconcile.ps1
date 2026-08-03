param(
    [Parameter(Mandatory = $true)][string]$AccessToken,
    [Parameter(Mandatory = $true)][string]$WorkspaceId,
    [string]$ApiUrl = "http://localhost:8000"
)
$headers = @{ Authorization = "Bearer $AccessToken"; "X-Workspace-ID" = $WorkspaceId }
Invoke-RestMethod -Method Post -Uri "$ApiUrl/api/v1/google-sheets/reconcile" -Headers $headers
