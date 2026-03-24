[CmdletBinding()]
param(
    [int]$SinceHours = 24,
    [int]$Limit = 500,
    [string]$Operation = '',
    [double]$MaxDegradedRate = 0.05,
    [double]$MinAvgTopScore = 0.45,
    [double]$MaxAvgLatencyMs = 1200,
    [string]$OutputDirectory = '',
    [switch]$FailOnViolation
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Invoke-CodexMemCliJson {
    param([Parameter(Mandatory = $true)][string[]]$CliArgs)
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
        $raw = & python @cmd 2>&1
    }
    finally {
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
    if (-not $text) { return @{} }
    return ($text | ConvertFrom-Json)
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
$baseline = Invoke-CodexMemCliJson -CliArgs $retrievalArgs

$items = @()
if ($baseline.items) { $items = @($baseline.items) }

$degradedCount = 0
$topScores = @()
$latencies = @()
foreach ($item in $items) {
    if ([bool]$item.degraded) { $degradedCount++ }
    $topScore = if ($null -eq $item.finalTopScore) { 0 } else { $item.finalTopScore }
    $latency = if ($null -eq $item.latencyMs) { 0 } else { $item.latencyMs }
    $topScores += [double]$topScore
    $latencies += [double]$latency
}

$count = [int]$items.Count
$degradedRate = if ($count -gt 0) { [math]::Round(($degradedCount / $count), 4) } else { 0.0 }
$avgTopScore = if ($count -gt 0) { [math]::Round((($topScores | Measure-Object -Average).Average), 4) } else { 0.0 }
$avgLatencyMs = if ($count -gt 0) { [math]::Round((($latencies | Measure-Object -Average).Average), 4) } else { 0.0 }

$violations = @()
if ($degradedRate -gt $MaxDegradedRate) {
    $violations += [pscustomobject]@{ metric = 'degradedRate'; actual = $degradedRate; threshold = $MaxDegradedRate; comparator = '>' }
}
if ($avgTopScore -lt $MinAvgTopScore) {
    $violations += [pscustomobject]@{ metric = 'avgFinalTopScore'; actual = $avgTopScore; threshold = $MinAvgTopScore; comparator = '<' }
}
if ($avgLatencyMs -gt $MaxAvgLatencyMs) {
    $violations += [pscustomobject]@{ metric = 'avgLatencyMs'; actual = $avgLatencyMs; threshold = $MaxAvgLatencyMs; comparator = '>' }
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$jsonPath = Join-Path $OutputDirectory "retrieval-alerts-$stamp.json"
$mdPath = Join-Path $OutputDirectory "retrieval-alerts-$stamp.md"

$result = [ordered]@{
    ok = $true
    generatedAt = [DateTime]::UtcNow.ToString('o')
    scope = @{
        sinceHours = $SinceHours
        limit = $Limit
        operation = $Operation
    }
    thresholds = @{
        maxDegradedRate = $MaxDegradedRate
        minAvgTopScore = $MinAvgTopScore
        maxAvgLatencyMs = $MaxAvgLatencyMs
    }
    metrics = @{
        retrievals = $count
        degradedRate = $degradedRate
        avgFinalTopScore = $avgTopScore
        avgLatencyMs = $avgLatencyMs
    }
    violations = $violations
    pass = ($violations.Count -eq 0)
}

$result | ConvertTo-Json -Depth 32 | Set-Content -Path $jsonPath -Encoding UTF8

$lines = @(
    '# Retrieval Alert Report',
    '',
    "Generated: $($result.generatedAt)",
    '',
    '## Scope',
    "- sinceHours: $SinceHours",
    "- limit: $Limit",
    "- operationFilter: $Operation",
    '',
    '## Thresholds',
    "- maxDegradedRate: $MaxDegradedRate",
    "- minAvgTopScore: $MinAvgTopScore",
    "- maxAvgLatencyMs: $MaxAvgLatencyMs",
    '',
    '## Metrics',
    "- retrievals: $count",
    "- degradedRate: $degradedRate",
    "- avgFinalTopScore: $avgTopScore",
    "- avgLatencyMs: $avgLatencyMs",
    '',
    '## Result',
    "- pass: $($result.pass)"
)

if ($violations.Count -gt 0) {
    $lines += ''
    $lines += '## Violations'
    foreach ($v in $violations) {
        $lines += "- $($v.metric): actual=$($v.actual) comparator=$($v.comparator) threshold=$($v.threshold)"
    }
}

Set-Content -Path $mdPath -Value $lines -Encoding UTF8

$output = [pscustomobject]@{
    ok = $true
    pass = $result.pass
    violationCount = $violations.Count
    jsonPath = $jsonPath
    markdownPath = $mdPath
    metrics = $result.metrics
}

$output | ConvertTo-Json -Depth 16

if ($FailOnViolation -and $violations.Count -gt 0) {
    throw "Telemetry alert threshold violation detected ($($violations.Count)). See $jsonPath"
}
