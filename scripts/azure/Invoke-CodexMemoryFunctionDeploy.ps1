[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroupName,
    [Parameter(Mandatory = $true)]
    [string]$FunctionAppName,
    [switch]$IncludeEmbeddingWorker,
    [switch]$AllowDegradedEmbedding
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Az {
    param([string[]]$CliArgs)
    $out = & az @CliArgs --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az failed: az $($CliArgs -join ' ')" }
    return $out
}

function Invoke-AzJson {
    param([string[]]$CliArgs)
    $out = & az @CliArgs --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) { throw "az failed: az $($CliArgs -join ' ')" }
    if ([string]::IsNullOrWhiteSpace($out)) { return $null }
    return ($out | ConvertFrom-Json)
}

function New-SignedHeaders {
    param(
        [hashtable]$Payload,
        [string]$FunctionKey,
        [string]$SharedSecret
    )
    $timestamp = (Get-Date).ToUniversalTime().ToString("o")
    $nonce = [Guid]::NewGuid().ToString("N")
    $signed = "{0}|{1}|{2}|{3}|{4}" -f $Payload.userId, $Payload.workspaceId, $Payload.repoId, $timestamp, $nonce
    $hmac = [System.Security.Cryptography.HMACSHA256]::new([System.Text.Encoding]::UTF8.GetBytes($SharedSecret))
    try {
        $signatureBytes = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($signed))
    }
    finally {
        $hmac.Dispose()
    }
    $signature = ([System.BitConverter]::ToString($signatureBytes)).Replace("-", "").ToLowerInvariant()
    return @{
        "x-functions-key" = $FunctionKey
        "Content-Type" = "application/json"
        "x-codex-context-timestamp" = $timestamp
        "x-codex-context-signature" = $signature
        "x-codex-context-nonce" = $nonce
    }
}

function New-EntraHeaders {
    param(
        [string]$FunctionKey,
        [string]$Scope
    )
    if ([string]::IsNullOrWhiteSpace($Scope)) {
        throw "MEMORY_AUTH_MODE=entra requires MEMORY_ENTRA_SCOPE app setting."
    }
    $token = (& az account get-access-token --scope $Scope --query accessToken -o tsv --only-show-errors)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$token)) {
        throw "Failed to acquire Entra token for scope: $Scope"
    }
    $headers = @{
        "Content-Type" = "application/json"
        "Authorization" = "Bearer $token"
    }
    if (-not [string]::IsNullOrWhiteSpace($FunctionKey)) {
        $headers["x-functions-key"] = $FunctionKey
    }
    return $headers
}

function Get-AppSettingValue {
    param(
        [object[]]$Settings,
        [string]$Name
    )
    $match = $Settings | Where-Object { $_.name -eq $Name } | Select-Object -First 1
    if ($null -eq $match) { return "" }
    return [string]$match.value
}

function Get-AppSettingsMap {
    param([object[]]$Settings)
    $map = @{}
    foreach ($entry in @($Settings)) {
        if ($entry.name) {
            $map[[string]$entry.name] = [string]$entry.value
        }
    }
    return $map
}

function Assert-RequiredEmbeddingSettings {
    param(
        [hashtable]$SettingsMap,
        [string]$Phase
    )
    $required = @(
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_EMBED_DEPLOYMENT"
    )
    $missing = @()
    foreach ($name in $required) {
        if (-not $SettingsMap.ContainsKey($name) -or [string]::IsNullOrWhiteSpace([string]$SettingsMap[$name])) {
            $missing += $name
        }
    }
    if ($missing.Count -gt 0) {
        throw "${Phase}: critical embedding settings missing: $($missing -join ', '). Refusing deploy cutover without semantic embedding configuration."
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$funcRoot = Join-Path $repoRoot "api"
$stage = Join-Path $env:TEMP ("ai-mem-deploy-stage-" + [guid]::NewGuid().ToString("n"))
$zipPath = Join-Path $env:TEMP ("ai-mem-deploy-" + (Get-Date -Format "yyyyMMddHHmmss") + ".zip")

$preSettings = Invoke-AzJson @("functionapp", "config", "appsettings", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName)
$preMap = Get-AppSettingsMap -Settings $preSettings
if (-not $AllowDegradedEmbedding) {
    Assert-RequiredEmbeddingSettings -SettingsMap $preMap -Phase "pre-deploy"
}

New-Item -ItemType Directory -Path $stage | Out-Null
Copy-Item (Join-Path $funcRoot "memory_api") (Join-Path $stage "memory_api") -Recurse
if ($IncludeEmbeddingWorker) {
    Copy-Item (Join-Path $funcRoot "embedding_worker") (Join-Path $stage "embedding_worker") -Recurse
}
Copy-Item (Join-Path $funcRoot "host.json") $stage
Copy-Item (Join-Path $funcRoot "requirements.txt") $stage
Get-ChildItem -Path $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

if (Test-Path $zipPath) { Remove-Item -Path $zipPath -Force }

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::CreateNew)
try {
    $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create, $false)
    try {
        $files = Get-ChildItem -Path $stage -Recurse -File
        foreach ($file in $files) {
            $rel = $file.FullName.Substring($stage.Length).TrimStart('\', '/')
            $entryName = $rel -replace '\\', '/'
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName, $entryName, [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    }
    finally {
        $zip.Dispose()
    }
}
finally {
    $fs.Dispose()
}

Write-Host ("Deploying package: " + $zipPath)
$deployment = Invoke-AzJson @(
    "functionapp", "deployment", "source", "config-zip",
    "-g", $ResourceGroupName,
    "-n", $FunctionAppName,
    "--src", $zipPath,
    "--build-remote", "true",
    "--timeout", "900"
)

$functions = Invoke-AzJson @("functionapp", "function", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName)
if (-not (@($functions | Where-Object { $_.name -match "/memory_api$" }).Count -ge 1)) {
    throw "memory_api function was not discovered after deployment."
}

$funcKey = Invoke-AzJson @("functionapp", "function", "keys", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName, "--function-name", "memory_api")
$defaultFuncKey = [string]$funcKey.default
if ([string]::IsNullOrWhiteSpace($defaultFuncKey)) {
    $hostKeys = Invoke-AzJson @("functionapp", "keys", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName)
    $defaultFuncKey = [string]$hostKeys.functionKeys.default
}
if ([string]::IsNullOrWhiteSpace($defaultFuncKey)) { throw "Could not fetch function key for smoke tests." }

$settings = Invoke-AzJson @("functionapp", "config", "appsettings", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName)
$settingsMap = Get-AppSettingsMap -Settings $settings
if (-not $AllowDegradedEmbedding) {
    Assert-RequiredEmbeddingSettings -SettingsMap $settingsMap -Phase "post-deploy"
}
$authMode = [string](Get-AppSettingValue -Settings $settings -Name "MEMORY_AUTH_MODE")
if ([string]::IsNullOrWhiteSpace($authMode)) { $authMode = "dual" }
$authMode = $authMode.Trim().ToLowerInvariant()
$entraScope = [string](Get-AppSettingValue -Settings $settings -Name "MEMORY_ENTRA_SCOPE")
$enableEntraInDual = [string](Get-AppSettingValue -Settings $settings -Name "MEMORY_ENABLE_ENTRA_IN_DUAL")
$allowAllEntra = [string](Get-AppSettingValue -Settings $settings -Name "MEMORY_ALLOW_ALL_ENTRA_PRINCIPALS")
$allowedObjects = [string](Get-AppSettingValue -Settings $settings -Name "MEMORY_ALLOWED_CALLER_OBJECT_IDS")
$allowedPrincipals = [string](Get-AppSettingValue -Settings $settings -Name "MEMORY_ALLOWED_CALLER_PRINCIPALS")
$sharedSecret = [string](Get-AppSettingValue -Settings $settings -Name "MEMORY_HMAC_SECRET")
if (($authMode -eq "shared_secret" -or $authMode -eq "dual") -and [string]::IsNullOrWhiteSpace($sharedSecret)) {
    throw "MEMORY_HMAC_SECRET missing for auth mode '$authMode'."
}
if ($authMode -eq "entra" -and [string]::IsNullOrWhiteSpace($entraScope)) {
    throw "MEMORY_ENTRA_SCOPE missing for auth mode 'entra'."
}
if (($authMode -eq "entra" -or (($authMode -eq "dual") -and ($enableEntraInDual -eq "true"))) -and ($allowAllEntra -ne "true") -and [string]::IsNullOrWhiteSpace($allowedObjects) -and [string]::IsNullOrWhiteSpace($allowedPrincipals)) {
    throw "Entra auth path requires MEMORY_ALLOWED_CALLER_OBJECT_IDS or MEMORY_ALLOWED_CALLER_PRINCIPALS unless MEMORY_ALLOW_ALL_ENTRA_PRINCIPALS=true."
}

$workspaceId = Get-AppSettingValue -Settings $settings -Name "WORKSPACE_ID"
if ([string]::IsNullOrWhiteSpace($workspaceId)) { $workspaceId = "<your-workspace>" }
$userId = Get-AppSettingValue -Settings $settings -Name "DEFAULT_USER_ID"
if ([string]::IsNullOrWhiteSpace($userId)) { $userId = "user@example.com" }
$repoId = "<your-workspace>"
$endpoint = "https://$FunctionAppName.azurewebsites.net"

function New-ApiHeaders {
    param([hashtable]$Payload)
    if ($authMode -eq "entra") {
        return New-EntraHeaders -FunctionKey $defaultFuncKey -Scope $entraScope
    }
    if ($authMode -eq "off") {
        $headers = @{ "Content-Type" = "application/json" }
        if (-not [string]::IsNullOrWhiteSpace($defaultFuncKey)) { $headers["x-functions-key"] = $defaultFuncKey }
        return $headers
    }
    return New-SignedHeaders -Payload $Payload -FunctionKey $defaultFuncKey -SharedSecret $sharedSecret
}

$sharedPayload = @{ userId = $userId; workspaceId = $workspaceId; repoId = $repoId; limit = 1 }
$sharedRsp = Invoke-RestMethod -Method Post -Uri "$endpoint/api/memory/memory_get_shared" -Headers (New-ApiHeaders -Payload $sharedPayload) -Body ($sharedPayload | ConvertTo-Json -Compress)

$routePayload = @{
    userId = $userId
    workspaceId = $workspaceId
    repoId = $repoId
    taskType = "architecture"
    riskLevel = "high"
    hasUnresolvedDisagreement = $true
}
$routeRsp = Invoke-RestMethod -Method Post -Uri "$endpoint/api/memory/memory_route_review" -Headers (New-ApiHeaders -Payload $routePayload) -Body ($routePayload | ConvertTo-Json -Compress)

$embedPayload = @{
    userId = $userId
    workspaceId = $workspaceId
    repoId = $repoId
    query = "deployment semantic embedding smoke"
    k = 1
}
$embedRsp = Invoke-RestMethod -Method Post -Uri "$endpoint/api/memory/memory_search_vectors" -Headers (New-ApiHeaders -Payload $embedPayload) -Body ($embedPayload | ConvertTo-Json -Compress)
if (-not $AllowDegradedEmbedding) {
    if ([string]$embedRsp.embeddingMode -ne "azure_openai" -or [bool]$embedRsp.degraded) {
        throw "Post-deploy semantic embedding gate failed. embeddingMode=$([string]$embedRsp.embeddingMode) degraded=$([bool]$embedRsp.degraded). Use -AllowDegradedEmbedding only for explicit emergency fallback."
    }
}

$result = [ordered]@{
    ok = $true
    functionApp = $FunctionAppName
    resourceGroup = $ResourceGroupName
    deployedPackage = $zipPath
    includeEmbeddingWorker = [bool]$IncludeEmbeddingWorker
    deploymentId = [string]$deployment.id
    deploymentStatus = [int]$deployment.status
    functionCount = @($functions).Count
    smoke = @{
        sharedOk = [bool]$sharedRsp.ok
        routeOk = [bool]$routeRsp.ok
        route = [string]$routeRsp.route
        embeddingMode = [string]$embedRsp.embeddingMode
        embeddingDegraded = [bool]$embedRsp.degraded
        embeddingWarnings = @($embedRsp.warnings)
        authMode = $authMode
    }
}

$result | ConvertTo-Json -Depth 8
