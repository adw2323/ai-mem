[CmdletBinding()]
param(
    [string]$Location = "<your-azure-region>",
    [string]$ResourceGroupName = "",
    [string]$CosmosAccountName = "",
    [string]$FunctionAppName = "",
    [string]$StorageAccountName = "",
    [string]$UserId = "user@example.com",
    [string]$WorkspaceId = "<your-workspace>",
    [string]$RepoId = "<your-workspace>",
    [ValidateSet("shared_secret", "entra", "dual", "off")]
    [string]$AuthMode = "dual",
    [string]$EntraScope = "",
    [switch]$IncludeRepoLocalDb
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Az {
    param([string[]]$CliArgs)
    $out = & az @CliArgs --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "az failed: az $($CliArgs -join ' ')"
    }
    return $out
}

function Invoke-AzJson {
    param([string[]]$CliArgs)
    $out = & az @CliArgs --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) { throw "az failed: az $($CliArgs -join ' ')" }
    if ([string]::IsNullOrWhiteSpace($out)) { return $null }
    return ($out | ConvertFrom-Json)
}

function Invoke-Python {
    param([string[]]$PythonArgs)
    & python @PythonArgs
    if ($LASTEXITCODE -ne 0) { throw "python failed: python $($PythonArgs -join ' ')" }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$funcRoot = Join-Path $repoRoot "api"
$clientPackageRoot = Join-Path $repoRoot "client"
$clientPackageSource = if ([string]::IsNullOrWhiteSpace($env:AI_MEM_CLIENT_PACKAGE)) { $clientPackageRoot } else { $env:AI_MEM_CLIENT_PACKAGE }
$migrationScript = Join-Path $repoRoot "scripts\migrate\sqlite_to_azure_memory.py"
$reportsDir = Join-Path $repoRoot "docs\reports"
$sharedSecret = [Guid]::NewGuid().ToString("N")
$embedQueueName = "embedding-jobs"
New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null

$sub = Invoke-AzJson @("account", "show")
if (-not $sub) { throw "No Azure account context. Run az login first." }

$suffix = (Get-Random -Minimum 1000 -Maximum 9999)
if ([string]::IsNullOrWhiteSpace($ResourceGroupName)) { $ResourceGroupName = "<your-resource-group>" }
if ([string]::IsNullOrWhiteSpace($CosmosAccountName)) { $CosmosAccountName = "<your-cosmos-account>$suffix" }
if ([string]::IsNullOrWhiteSpace($FunctionAppName)) { $FunctionAppName = "<your-function-app>-$suffix" }
if ([string]::IsNullOrWhiteSpace($StorageAccountName)) { $StorageAccountName = ("<your-storage-account>$suffix").ToLowerInvariant() }
$tags = @("createdBy=$UserId", "workload=ai-mem", "env=prod")

Write-Host "[1/9] Ensure resource group $ResourceGroupName in $Location"
Invoke-Az (@("group", "create", "--name", $ResourceGroupName, "--location", $Location, "--tags") + $tags) | Out-Null

Write-Host "[2/9] Provision Cosmos DB account + database/containers"
$cosmosCreated = $false
try {
    Invoke-Az @(
        "cosmosdb", "create",
        "--name", $CosmosAccountName,
        "--resource-group", $ResourceGroupName,
        "--locations", "regionName=$Location", "failoverPriority=0",
        "--default-consistency-level", "Session",
        "--enable-free-tier", "true",
        "--kind", "GlobalDocumentDB",
        "--tags", "createdBy=$UserId", "workload=ai-mem", "env=prod"
    ) | Out-Null
    $cosmosCreated = $true
}
catch {
    Write-Host "Cosmos free tier create failed, retrying without free tier (likely already consumed in subscription)."
    Invoke-Az @(
        "cosmosdb", "create",
        "--name", $CosmosAccountName,
        "--resource-group", $ResourceGroupName,
        "--locations", "regionName=$Location", "failoverPriority=0",
        "--default-consistency-level", "Session",
        "--kind", "GlobalDocumentDB",
        "--tags", "createdBy=$UserId", "workload=ai-mem", "env=prod"
    ) | Out-Null
}

Invoke-Az @("cosmosdb", "sql", "database", "create", "-a", $CosmosAccountName, "-g", $ResourceGroupName, "-n", "ai_mem") | Out-Null
Invoke-Az @("cosmosdb", "sql", "container", "create", "-a", $CosmosAccountName, "-g", $ResourceGroupName, "-d", "ai_mem", "-n", "personal_memory", "-p", "/userId") | Out-Null
Invoke-Az @("cosmosdb", "sql", "container", "create", "-a", $CosmosAccountName, "-g", $ResourceGroupName, "-d", "ai_mem", "-n", "shared_memory", "-p", "/workspaceId") | Out-Null
Invoke-Az @("cosmosdb", "sql", "container", "create", "-a", $CosmosAccountName, "-g", $ResourceGroupName, "-d", "ai_mem", "-n", "audit_log", "-p", "/workspaceId") | Out-Null
Invoke-Az @("cosmosdb", "sql", "container", "create", "-a", $CosmosAccountName, "-g", $ResourceGroupName, "-d", "ai_mem", "-n", "embeddings", "-p", "/workspaceId") | Out-Null
Invoke-Az @("cosmosdb", "sql", "container", "create", "-a", $CosmosAccountName, "-g", $ResourceGroupName, "-d", "ai_mem", "-n", "retrieval_log", "-p", "/workspaceId") | Out-Null

$cosmos = Invoke-AzJson @("cosmosdb", "show", "-n", $CosmosAccountName, "-g", $ResourceGroupName)
$cosmosEndpoint = [string]$cosmos.documentEndpoint

Write-Host "[3/9] Provision storage + function app"
Invoke-Az (@("storage", "account", "create", "-n", $StorageAccountName, "-g", $ResourceGroupName, "-l", $Location, "--sku", "Standard_LRS", "--kind", "StorageV2", "--allow-blob-public-access", "false", "--min-tls-version", "TLS1_2", "--tags") + $tags) | Out-Null
Invoke-Az @(
    "functionapp", "create",
    "--name", $FunctionAppName,
    "--resource-group", $ResourceGroupName,
    "--storage-account", $StorageAccountName,
    "--consumption-plan-location", $Location,
    "--runtime", "python",
    "--runtime-version", "3.12",
    "--functions-version", "4",
    "--os-type", "Linux",
    "--tags", "createdBy=$UserId", "workload=ai-mem", "env=prod"
) | Out-Null
Invoke-Az @("functionapp", "update", "-g", $ResourceGroupName, "-n", $FunctionAppName, "--set", "httpsOnly=true") | Out-Null
$storageConnectionString = [string](Invoke-AzJson @("storage", "account", "show-connection-string", "-n", $StorageAccountName, "-g", $ResourceGroupName)).connectionString
& az storage queue create --name $embedQueueName --connection-string $storageConnectionString --only-show-errors | Out-Null

Write-Host "[4/9] Enable managed identity + Cosmos RBAC assignment"
$identity = Invoke-AzJson @("functionapp", "identity", "assign", "-g", $ResourceGroupName, "-n", $FunctionAppName)
$principalId = [string]$identity.principalId
$roles = Invoke-AzJson @("cosmosdb", "sql", "role", "definition", "list", "-a", $CosmosAccountName, "-g", $ResourceGroupName)
$dataContributor = $roles | Where-Object { $_.roleName -eq "Cosmos DB Built-in Data Contributor" } | Select-Object -First 1
if (-not $dataContributor) { throw "Could not resolve Cosmos DB Built-in Data Contributor role." }
Invoke-Az @(
    "cosmosdb", "sql", "role", "assignment", "create",
    "-a", $CosmosAccountName,
    "-g", $ResourceGroupName,
    "--role-definition-id", $dataContributor.id,
    "--principal-id", $principalId,
    "--scope", "/"
) | Out-Null

Write-Host "[5/9] Configure function app settings"
Invoke-Az @(
    "functionapp", "config", "appsettings", "set",
    "-g", $ResourceGroupName,
    "-n", $FunctionAppName,
    "--settings",
    "COSMOS_ENDPOINT=$cosmosEndpoint",
    "COSMOS_DB_NAME=ai_mem",
    "FUNCTIONS_WORKER_RUNTIME=python",
    "WEBSITE_RUN_FROM_PACKAGE=1",
    "MEMORY_HMAC_SECRET=$sharedSecret",
    "MEMORY_REQUIRE_SIGNED_CONTEXT=true",
    "MEMORY_REQUIRE_SIGNED_NONCE=true",
    "MEMORY_AUTH_MODE=$AuthMode",
    "MEMORY_ALLOW_INSECURE_AUTH_OFF=false",
    "MEMORY_ENABLE_ENTRA_IN_DUAL=false",
    "MEMORY_ALLOW_ALL_ENTRA_PRINCIPALS=false",
    "MEMORY_ENTRA_SCOPE=$EntraScope",
    "MEMORY_ALLOWED_CALLER_OBJECT_IDS=",
    "MEMORY_ALLOWED_CALLER_PRINCIPALS=",
    "EMBEDDING_WRITE_MODE=queue",
    "MEMORY_EMBED_QUEUE_NAME=$embedQueueName",
    "AUTO_EXTRACT_RUN_SUMMARIES=false"
) | Out-Null

Write-Host "[6/9] Deploy function app package (remote build)"
$zipPath = Join-Path $env:TEMP ("ai-mem-api-{0}.zip" -f (Get-Date -Format "yyyyMMddHHmmss"))
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Push-Location $funcRoot
try {
    Compress-Archive -Path * -DestinationPath $zipPath -CompressionLevel Optimal -Force
}
finally {
    Pop-Location
}
Invoke-Az @("functionapp", "deployment", "source", "config-zip", "-g", $ResourceGroupName, "-n", $FunctionAppName, "--src", $zipPath, "--build-remote", "true", "--timeout", "900") | Out-Null

Write-Host "[7/9] Install portable client package + configure Codex"
if (($clientPackageSource -eq $clientPackageRoot) -and (-not (Test-Path $clientPackageRoot))) { throw "Client package not found: $clientPackageRoot" }
Invoke-Python -PythonArgs @("-m", "pip", "install", "--user", "--upgrade", $clientPackageSource)

$funcKey = Invoke-AzJson @("functionapp", "function", "keys", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName, "--function-name", "memory_api")
$defaultFuncKey = [string]$funcKey.default
if ([string]::IsNullOrWhiteSpace($defaultFuncKey)) {
    $hostKeys = Invoke-AzJson @("functionapp", "keys", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName)
    $defaultFuncKey = [string]$hostKeys.functionKeys.default
}
if ([string]::IsNullOrWhiteSpace($defaultFuncKey)) { throw "Could not fetch function key." }
$functionEndpoint = "https://$FunctionAppName.azurewebsites.net"

$installArgs = @("-m", "ai_mem.cli", "install-mcp", "--endpoint", $functionEndpoint, "--function-key", $defaultFuncKey, "--shared-secret", $sharedSecret, "--auth-mode", $AuthMode, "--user-id", $UserId, "--workspace-id", $WorkspaceId, "--repo-id", $RepoId)
if (-not [string]::IsNullOrWhiteSpace($EntraScope)) { $installArgs += @("--entra-scope", $EntraScope) }
Invoke-Python -PythonArgs $installArgs | Out-Null

Write-Host "[8/9] Run SQLite backup + migration"
$primaryDb = Join-Path $env:USERPROFILE ".codex\codexmem.db"
$dbArgs = @("--db", $primaryDb)
if ($IncludeRepoLocalDb) {
    $repoDb = Join-Path $repoRoot "codex-memory\data\codexmem.db"
    if (Test-Path $repoDb) { $dbArgs += @("--db", $repoDb) }
}
$reportPath = Join-Path $reportsDir ("migration-report-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
& python $migrationScript `
    --endpoint $functionEndpoint `
    --function-key $defaultFuncKey `
    --shared-secret $sharedSecret `
    --user-id $UserId `
    --workspace-id $WorkspaceId `
    --repo-id $RepoId `
    @dbArgs `
    --report $reportPath
if ($LASTEXITCODE -ne 0) { throw "Migration script failed." }

Write-Host "[9/9] Cleanup wrapper interception remnants"
$profilePath = $PROFILE.CurrentUserAllHosts
if (Test-Path $profilePath) {
    $raw = Get-Content $profilePath -Raw
    $raw = [Regex]::Replace($raw, '(?ms)# >>> codexmem override >>>.*?# <<< codexmem override <<<\s*', "")
    Set-Content -LiteralPath $profilePath -Value $raw -Encoding ascii
}

# Local validation calls
function Get-SignedHeader {
    param([hashtable]$Payload)
    $timestamp = (Get-Date).ToUniversalTime().ToString("o")
    $nonce = [Guid]::NewGuid().ToString("N")
    $signed = "{0}|{1}|{2}|{3}|{4}" -f $Payload.userId, $Payload.workspaceId, $Payload.repoId, $timestamp, $nonce
    $hmac = [System.Security.Cryptography.HMACSHA256]::new([System.Text.Encoding]::UTF8.GetBytes($sharedSecret))
    try {
        $signatureBytes = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($signed))
    }
    finally {
        $hmac.Dispose()
    }
    $signature = ([System.BitConverter]::ToString($signatureBytes)).Replace("-", "").ToLowerInvariant()
    return @{
        "x-functions-key" = $defaultFuncKey
        "Content-Type" = "application/json"
        "x-codex-context-timestamp" = $timestamp
        "x-codex-context-signature" = $signature
        "x-codex-context-nonce" = $nonce
    }
}

$sharedPayload = @{ userId = $UserId; workspaceId = $WorkspaceId; repoId = $RepoId; limit = 3 }
$shared = Invoke-RestMethod -Method Post -Uri "$functionEndpoint/api/memory/memory_get_shared" -Headers (Get-SignedHeader -Payload $sharedPayload) -Body ($sharedPayload | ConvertTo-Json -Compress)
$personal = Invoke-RestMethod -Method Post -Uri "$functionEndpoint/api/memory/memory_get_personal" -Headers (Get-SignedHeader -Payload $sharedPayload) -Body ($sharedPayload | ConvertTo-Json -Compress)
$vectorPayload = @{ userId = $UserId; workspaceId = $WorkspaceId; repoId = $RepoId; query = "ai-mem migration"; k = 3 }
$vect = Invoke-RestMethod -Method Post -Uri "$functionEndpoint/api/memory/memory_search_vectors" -Headers (Get-SignedHeader -Payload $vectorPayload) -Body ($vectorPayload | ConvertTo-Json -Compress)
$summaries = Invoke-RestMethod -Method Post -Uri "$functionEndpoint/api/memory/memory_search_summaries" -Headers (Get-SignedHeader -Payload $vectorPayload) -Body ($vectorPayload | ConvertTo-Json -Compress)
$contextPayload = @{ userId = $UserId; workspaceId = $WorkspaceId; repoId = $RepoId; query = "ai-mem migration"; budget = "small"; k = 3 }
$context = Invoke-RestMethod -Method Post -Uri "$functionEndpoint/api/memory/memory_build_context" -Headers (Get-SignedHeader -Payload $contextPayload) -Body ($contextPayload | ConvertTo-Json -Compress)

$summary = [ordered]@{
    timestamp = (Get-Date).ToString("s")
    location = $Location
    resourceGroup = $ResourceGroupName
    cosmosAccount = $CosmosAccountName
    cosmosFreeTierRequested = $cosmosCreated
    functionApp = $FunctionAppName
    functionEndpoint = $functionEndpoint
    migrationReport = $reportPath
    validation = @{
        sharedCount = @($shared.items).Count
        personalCount = @($personal.items).Count
        vectorCount = @($vect.items).Count
        summaryCount = @($summaries.items).Count
        contextItemCount = [int]$context.itemCount
        vectorEmbeddingMode = [string]$vect.embeddingMode
        contextTelemetryId = [string]$context.telemetryId
    }
}

$summaryPath = Join-Path $reportsDir ("deployment-summary-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding utf8
Write-Host "Build complete."
Write-Host ($summary | ConvertTo-Json -Depth 8)
