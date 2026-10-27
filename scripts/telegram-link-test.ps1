param(
    [Parameter(Mandatory = $true)][string]$AccessToken,
    [Parameter(Mandatory = $true)][string]$WorkspaceId,
    [Parameter(Mandatory = $true)][long]$TelegramUserId,
    [Parameter(Mandatory = $true)][long]$TelegramChatId,
    [string]$BackendUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Stop"
$userHeaders = @{ Authorization = "Bearer $AccessToken"; "X-Workspace-ID" = $WorkspaceId }
$linkCode = Invoke-RestMethod -Method Post -Uri "$BackendUrl/api/v1/settings/telegram/link-code" -Headers $userHeaders
$serviceKey = Read-Host "Введите ServiceKey n8n" -AsSecureString
$plainKey = [System.Net.NetworkCredential]::new("", $serviceKey).Password
$serviceHeaders = @{
    Authorization = "ServiceKey $plainKey"
    "X-Idempotency-Key" = "telegram-link-test:$([guid]::NewGuid())"
}
$body = @{
    code = $linkCode.code
    telegram_user_id = $TelegramUserId
    telegram_chat_id = $TelegramChatId
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$BackendUrl/api/v1/integrations/telegram/link" -Headers $serviceHeaders -ContentType "application/json" -Body $body
$plainKey = $null
