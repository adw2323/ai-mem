[CmdletBinding()]
param(
    [int]$SampleLimit = 250,
    [int]$MaxPendingAgeMinutes = 30,
    [switch]$ReplayStale,
    [int]$ReplayLimit = 250,
    [string]$ReplayScope = 'all',
    [switch]$ClearPoisonQueue,
    [switch]$RunProbe,
    [int]$ProbeTimeoutSeconds = 120,
    [int]$ProbePollSeconds = 10,
    [string]$ResourceGroupName,
    [string]$FunctionAppName
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$clientSrc = Join-Path $repoRoot 'client\src'

function Invoke-CodexMemCliJson {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$CliArgs
    )
    $cmd = @('-m', 'ai_mem.cli') + $CliArgs
    $previousPythonPath = $env:PYTHONPATH
    if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        $env:PYTHONPATH = $clientSrc
    }
    elseif (-not $previousPythonPath.Split([IO.Path]::PathSeparator) -contains $clientSrc) {
        $env:PYTHONPATH = "$clientSrc$([IO.Path]::PathSeparator)$previousPythonPath"
    }
    try {
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $raw = & python @cmd 2>&1
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
        if ($null -eq $previousPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        }
        else {
            $env:PYTHONPATH = $previousPythonPath
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "ai-mem cli failed for args [$($CliArgs -join ' ')]: $($raw -join "`n")"
    }
    $text = ($raw -join "`n").Trim()
    if (-not $text) {
        return @{}
    }
    return ($text | ConvertFrom-Json)
}

function Get-ApproximateQueueCount {
    param(
        [string]$Rg,
        [string]$App
    )
    if (-not $Rg -or -not $App) {
        return $null
    }
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $settings = az functionapp config appsettings list --resource-group $Rg --name $App --output json 2>$null
    $ErrorActionPreference = $previousErrorAction
    if ($LASTEXITCODE -ne 0 -or -not $settings) {
        return $null
    }
    $pairs = $settings | ConvertFrom-Json
    $settingsMap = @{}
    foreach ($pair in $pairs) {
        if ($pair.name) {
            $settingsMap[$pair.name] = $pair.value
        }
    }
    if (-not $settingsMap.ContainsKey('AzureWebJobsStorage')) {
        return $null
    }
    $queueName = $settingsMap['MEMORY_EMBED_QUEUE_NAME']
    if ([string]::IsNullOrWhiteSpace($queueName)) {
        $queueName = 'embedding-jobs'
    }
    return @{
        queueName = $queueName
        connectionString = $settingsMap['AzureWebJobsStorage']
    }
}

function Get-QueuePeekCount {
    param(
        [string]$QueueName,
        [string]$ConnectionString,
        [int]$NumMessages = 32
    )
    if ([string]::IsNullOrWhiteSpace($QueueName) -or [string]::IsNullOrWhiteSpace($ConnectionString)) {
        return $null
    }
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $json = az storage message peek --queue-name $QueueName --connection-string $ConnectionString --num-messages $NumMessages -o json 2>$null
    $ErrorActionPreference = $previousErrorAction
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        return $null
    }
    $items = $json | ConvertFrom-Json
    if ($null -eq $items) { return 0 }
    return @($items).Count
}

function Parse-IsoDate {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    try {
        return ([DateTimeOffset]::Parse($Value)).UtcDateTime
    }
    catch {
        return $null
    }
}

function Get-StringValue {
    param(
        [object]$Value,
        [string]$Default = ''
    )
    if ($null -eq $Value) { return $Default }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { return $Default }
    return $text
}

function Get-EmbeddingSnapshot {
    param(
        [int]$Limit,
        [int]$PendingAgeMinutes
    )
    $sharedItems = Invoke-CodexMemCliJson -CliArgs @('get-shared', '--limit', "$Limit")
    $personalItems = Invoke-CodexMemCliJson -CliArgs @('get-personal', '--limit', "$Limit")
    $all = @()
    if ($sharedItems.items) { $all += @($sharedItems.items) }
    if ($personalItems.items) { $all += @($personalItems.items) }
    $nowUtc = [DateTime]::UtcNow
    $maxAge = [TimeSpan]::FromMinutes($PendingAgeMinutes)
    $pendingItems = @()
    $failedItems = @()
    $readyItems = @()
    $staleItems = @()
    foreach ($entry in $all) {
        $status = Get-StringValue -Value $entry.embeddingStatus -Default 'unknown'
        if ($status -eq 'ready') {
            $readyItems += $entry
        }
        elseif ($status -eq 'failed') {
            $failedItems += $entry
        }
        elseif ($status -eq 'pending') {
            $pendingItems += $entry
            $updatedRaw = Get-StringValue -Value $entry.updatedAt -Default (Get-StringValue -Value $entry.createdAt)
            $updated = Parse-IsoDate -Value $updatedRaw
            if ($updated -and (($nowUtc - $updated) -gt $maxAge)) {
                $staleItems += $entry
            }
        }
    }
    return @{
        items = $all
        pending = $pendingItems
        failed = $failedItems
        ready = $readyItems
        stalePending = $staleItems
    }
}

$initial = Get-EmbeddingSnapshot -Limit $SampleLimit -PendingAgeMinutes $MaxPendingAgeMinutes
$stalePending = @($initial.stalePending)

$repair = $null
if ($ReplayStale -and $stalePending.Count -gt 0) {
    $repair = Invoke-CodexMemCliJson -CliArgs @('rebuild-embeddings', '--scope', $ReplayScope, '--limit', "$ReplayLimit")
}

$probe = $null
if ($RunProbe) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
    $probeResult = Invoke-CodexMemCliJson -CliArgs @(
        'add-run',
        '--branch', 'embedding-health-probe',
        '--cwd', 'ai-mem-health',
        '--request', "probe-run-$stamp",
        '--summary', "Embedding worker probe created at $stamp UTC",
        '--status', 'completed',
        '--memory-scope', 'workspace',
        '--memory-class', 'episodic',
        '--promotion-status', 'candidate'
    )
    $probeId = [string]$probeResult.id
    $deadline = [DateTime]::UtcNow.AddSeconds($ProbeTimeoutSeconds)
    $observed = ''
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Seconds $ProbePollSeconds
        $personalItems = Invoke-CodexMemCliJson -CliArgs @('get-personal', '--limit', "$SampleLimit")
        $matched = @($personalItems.items | Where-Object { $_.id -eq $probeId } | Select-Object -First 1)
        if ($matched.Count -gt 0) {
            $observed = Get-StringValue -Value $matched[0].embeddingStatus -Default 'unknown'
            if ($observed -eq 'ready' -or $observed -eq 'failed') {
                break
            }
        }
    }
    $probe = @{
        id = $probeId
        initialStatus = $probeResult.embeddingStatus
        finalStatus = if ($observed) { $observed } else { 'timeout' }
        timeoutSeconds = $ProbeTimeoutSeconds
    }
}

$final = Get-EmbeddingSnapshot -Limit $SampleLimit -PendingAgeMinutes $MaxPendingAgeMinutes
$items = @($final.items)
$pending = @($final.pending)
$failed = @($final.failed)
$ready = @($final.ready)
$stalePending = @($final.stalePending)

$queueInfo = Get-ApproximateQueueCount -Rg $ResourceGroupName -App $FunctionAppName
$queuePeek = $null
$poisonPeek = $null
if ($queueInfo -and $queueInfo.connectionString) {
    $queuePeek = Get-QueuePeekCount -QueueName $queueInfo.queueName -ConnectionString $queueInfo.connectionString
    $poisonPeek = Get-QueuePeekCount -QueueName "$($queueInfo.queueName)-poison" -ConnectionString $queueInfo.connectionString
    if ($ClearPoisonQueue -and $poisonPeek -gt 0) {
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        az storage message clear --queue-name "$($queueInfo.queueName)-poison" --connection-string $queueInfo.connectionString -o none 2>$null | Out-Null
        $ErrorActionPreference = $previousErrorAction
        $poisonPeek = Get-QueuePeekCount -QueueName "$($queueInfo.queueName)-poison" -ConnectionString $queueInfo.connectionString
    }
}
$result = @{
    ok = $true
    generatedAt = [DateTime]::UtcNow.ToString('o')
    sampleLimit = $SampleLimit
    maxPendingAgeMinutes = $MaxPendingAgeMinutes
    totals = @{
        sampled = $items.Count
        ready = $ready.Count
        pending = $pending.Count
        failed = $failed.Count
        stalePending = $stalePending.Count
    }
    queue = @{
        queueName = if ($queueInfo) { $queueInfo.queueName } else { '' }
        peekCount = $queuePeek
        poisonPeekCount = $poisonPeek
        source = if ($queuePeek -ne $null -or $poisonPeek -ne $null) { 'azure_storage_message_peek' } else { 'unavailable' }
    }
    stalePendingIds = @($stalePending | Select-Object -ExpandProperty id)
    failedIds = @($failed | Select-Object -ExpandProperty id)
}
if ($repair) { $result.repair = $repair }
if ($probe) { $result.probe = $probe }

$result | ConvertTo-Json -Depth 64
