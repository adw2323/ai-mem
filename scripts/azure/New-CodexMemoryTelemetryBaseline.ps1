[CmdletBinding()]
param(
    [int]$SinceHours = 24,
    [int]$Limit = 500,
    [string]$Operation = '',
    [string]$OutputDirectory = '',
    [switch]$IncludeRaw
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Invoke-CodexMemCliJson {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$CliArgs
    )
    $cmd = @('-m', 'ai_mem.cli') + $CliArgs
    $clientSrc = Join-Path $repoRoot 'client\src'
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

function Safe-Average {
    param([double[]]$Values)
    if (-not $Values -or $Values.Count -eq 0) { return 0.0 }
    return [Math]::Round((($Values | Measure-Object -Average).Average), 4)
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

function Get-NumberValue {
    param(
        [object]$Value,
        [double]$Default = 0
    )
    if ($null -eq $Value) { return $Default }
    $parsed = 0.0
    if ([double]::TryParse([string]$Value, [ref]$parsed)) {
        return $parsed
    }
    return $Default
}

function Get-BoolValue {
    param(
        [object]$Value,
        [bool]$Default = $false
    )
    if ($null -eq $Value) { return $Default }
    if ($Value -is [bool]) { return [bool]$Value }
    $text = ([string]$Value).Trim().ToLowerInvariant()
    if ($text -in @('1', 'true', 'yes', 'on')) { return $true }
    if ($text -in @('0', 'false', 'no', 'off')) { return $false }
    return $Default
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot 'docs\reports'
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$retrievalArgs = @(
    'retrieval-logs',
    '--limit', "$Limit",
    '--since-hours', "$SinceHours"
)
if (-not [string]::IsNullOrWhiteSpace($Operation)) {
    $retrievalArgs += @('--operation', "$Operation")
}
$logsResult = Invoke-CodexMemCliJson -CliArgs $retrievalArgs
$items = @()
if ($logsResult.items) { $items = @($logsResult.items) }

$opGroups = @{}
$intentGroups = @{}
$degradedCount = 0
$topScoreValues = @()
$latencyValues = @()

foreach ($item in $items) {
    $op = Get-StringValue -Value $item.operation -Default 'unknown'
    $intent = Get-StringValue -Value $item.intent -Default 'unknown'
    $degraded = Get-BoolValue -Value $item.degraded -Default $false
    $latency = Get-NumberValue -Value $item.latencyMs -Default 0
    $topScore = Get-NumberValue -Value $item.finalTopScore -Default 0

    if (-not $opGroups.ContainsKey($op)) {
        $opGroups[$op] = @{ count = 0; degraded = 0; latency = @(); top = @() }
    }
    if (-not $intentGroups.ContainsKey($intent)) {
        $intentGroups[$intent] = @{ count = 0; degraded = 0; latency = @(); top = @() }
    }

    $opGroups[$op].count++
    $intentGroups[$intent].count++
    $opGroups[$op].latency += $latency
    $intentGroups[$intent].latency += $latency
    $opGroups[$op].top += $topScore
    $intentGroups[$intent].top += $topScore
    if ($degraded) {
        $degradedCount++
        $opGroups[$op].degraded++
        $intentGroups[$intent].degraded++
    }
    $latencyValues += $latency
    $topScoreValues += $topScore
}

$opSummary = @()
foreach ($key in ($opGroups.Keys | Sort-Object)) {
    $entry = $opGroups[$key]
    $opSummary += [pscustomobject]@{
        operation = $key
        count = [int]$entry.count
        degradedRate = if ($entry.count -gt 0) { [Math]::Round(($entry.degraded / $entry.count), 4) } else { 0.0 }
        avgLatencyMs = Safe-Average -Values $entry.latency
        avgFinalTopScore = Safe-Average -Values $entry.top
    }
}

$intentSummary = @()
foreach ($key in ($intentGroups.Keys | Sort-Object)) {
    $entry = $intentGroups[$key]
    $intentSummary += [pscustomobject]@{
        intent = $key
        count = [int]$entry.count
        degradedRate = if ($entry.count -gt 0) { [Math]::Round(($entry.degraded / $entry.count), 4) } else { 0.0 }
        avgLatencyMs = Safe-Average -Values $entry.latency
        avgFinalTopScore = Safe-Average -Values $entry.top
    }
}

$baseline = [ordered]@{
    ok = $true
    generatedAt = [DateTime]::UtcNow.ToString('o')
    scope = @{
        sinceHours = $SinceHours
        limit = $Limit
        operation = $Operation
    }
    totals = @{
        retrievals = $items.Count
        degradedCount = $degradedCount
        degradedRate = if ($items.Count -gt 0) { [Math]::Round(($degradedCount / $items.Count), 4) } else { 0.0 }
        avgLatencyMs = Safe-Average -Values $latencyValues
        avgFinalTopScore = Safe-Average -Values $topScoreValues
    }
    byOperation = $opSummary
    byIntent = $intentSummary
}
if ($IncludeRaw) {
    $baseline['raw'] = $items
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$jsonPath = Join-Path $OutputDirectory "retrieval-baseline-$stamp.json"
$mdPath = Join-Path $OutputDirectory "retrieval-baseline-$stamp.md"

$baseline | ConvertTo-Json -Depth 64 | Set-Content -Path $jsonPath -Encoding UTF8

$lines = @(
    '# Retrieval Baseline Report',
    '',
    "Generated: $($baseline.generatedAt)",
    '',
    '## Scope',
    "- sinceHours: $SinceHours",
    "- limit: $Limit",
    "- operationFilter: $Operation",
    '',
    '## Totals',
    "- retrievals: $($baseline.totals.retrievals)",
    "- degradedCount: $($baseline.totals.degradedCount)",
    "- degradedRate: $($baseline.totals.degradedRate)",
    "- avgLatencyMs: $($baseline.totals.avgLatencyMs)",
    "- avgFinalTopScore: $($baseline.totals.avgFinalTopScore)",
    '',
    '## By Operation'
)

foreach ($row in $opSummary) {
    $lines += "- $($row.operation): count=$($row.count), degradedRate=$($row.degradedRate), avgLatencyMs=$($row.avgLatencyMs), avgFinalTopScore=$($row.avgFinalTopScore)"
}

$lines += ''
$lines += '## By Intent'
foreach ($row in $intentSummary) {
    $lines += "- $($row.intent): count=$($row.count), degradedRate=$($row.degradedRate), avgLatencyMs=$($row.avgLatencyMs), avgFinalTopScore=$($row.avgFinalTopScore)"
}

Set-Content -Path $mdPath -Value $lines -Encoding UTF8

[pscustomobject]@{
    ok = $true
    jsonPath = $jsonPath
    markdownPath = $mdPath
    retrievals = $items.Count
    degradedRate = $baseline.totals.degradedRate
    avgLatencyMs = $baseline.totals.avgLatencyMs
    avgFinalTopScore = $baseline.totals.avgFinalTopScore
} | ConvertTo-Json -Depth 16
