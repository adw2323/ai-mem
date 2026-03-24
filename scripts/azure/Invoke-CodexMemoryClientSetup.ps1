[CmdletBinding()]
param(
    [string]$ResourceGroupName = "<your-resource-group>",
    [string]$FunctionAppName = "<your-function-app>",
    [string]$UserId = "",
    [string]$WorkspaceId = "<your-workspace>",
    [string]$RepoId = "<your-workspace>",
    [ValidateSet("shared_secret", "entra", "dual", "off")]
    [string]$AuthMode = "dual",
    [string]$EntraScope = "",
    [ValidateSet("codex", "abacus", "both")]
    [string]$Target = "both"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-AzJson {
    param([string[]]$CliArgs)
    $out = & az @CliArgs --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) { throw "az failed: az $($CliArgs -join ' ')" }
    if ([string]::IsNullOrWhiteSpace($out)) { return $null }
    return ($out | ConvertFrom-Json)
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$clientPackageRoot = Join-Path $repoRoot "client"
$clientPackageSource = if ([string]::IsNullOrWhiteSpace($env:AI_MEM_CLIENT_PACKAGE)) { $clientPackageRoot } else { $env:AI_MEM_CLIENT_PACKAGE }

function Invoke-Python {
    param([string[]]$PythonArgs)
    & python @PythonArgs
    if ($LASTEXITCODE -ne 0) { throw "python failed: python $($PythonArgs -join ' ')" }
}

if ([string]::IsNullOrWhiteSpace($UserId)) {
    $UserId = $env:USERPRINCIPALNAME
    if ([string]::IsNullOrWhiteSpace($UserId)) {
        $UserId = $env:USERNAME
    }
}
if ([string]::IsNullOrWhiteSpace($UserId)) { throw "UserId could not be resolved. Pass -UserId explicitly." }

Write-Host "[1/4] Verify Azure session"
$sub = Invoke-AzJson @("account", "show")
if (-not $sub) { throw "No Azure account context. Run az login first." }

Write-Host "[2/4] Fetch function endpoint and key"
$funcKey = Invoke-AzJson @("functionapp", "function", "keys", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName, "--function-name", "memory_api")
$defaultFuncKey = [string]$funcKey.default
if ([string]::IsNullOrWhiteSpace($defaultFuncKey)) {
    $hostKeys = Invoke-AzJson @("functionapp", "keys", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName)
    $defaultFuncKey = [string]$hostKeys.functionKeys.default
}
if ([string]::IsNullOrWhiteSpace($defaultFuncKey)) { throw "Could not fetch function key." }
$settings = Invoke-AzJson @("functionapp", "config", "appsettings", "list", "-g", $ResourceGroupName, "-n", $FunctionAppName)
$sharedSecret = [string](($settings | Where-Object { $_.name -eq "MEMORY_HMAC_SECRET" } | Select-Object -First 1).value)
if (($AuthMode -in @("shared_secret", "dual")) -and [string]::IsNullOrWhiteSpace($sharedSecret)) { throw "Could not fetch MEMORY_HMAC_SECRET app setting." }
$functionEndpoint = "https://$FunctionAppName.azurewebsites.net"
Write-Host "  Endpoint : $functionEndpoint"
Write-Host "  Key      : $($defaultFuncKey.Substring(0, 8))..."

Write-Host "[3/4] Install portable ai-mem client package"
if (($clientPackageSource -eq $clientPackageRoot) -and (-not (Test-Path $clientPackageRoot))) { throw "Client package not found: $clientPackageRoot" }
Invoke-Python -PythonArgs @("-m", "pip", "install", "--user", "--upgrade", $clientPackageSource)
$mcpDir = Join-Path $env:USERPROFILE ".ai-mem\mcp"
New-Item -ItemType Directory -Force -Path $mcpDir | Out-Null

if ($Target -in @("codex", "both")) {
    Write-Host "[4/4] Update ~/.codex/config.toml"
    $installArgs = @("-m", "ai_mem.cli", "install-mcp", "--endpoint", $functionEndpoint, "--function-key", $defaultFuncKey, "--shared-secret", $sharedSecret, "--auth-mode", $AuthMode, "--user-id", $UserId, "--workspace-id", $WorkspaceId, "--repo-id", $RepoId)
    if (-not [string]::IsNullOrWhiteSpace($EntraScope)) { $installArgs += @("--entra-scope", $EntraScope) }
    Invoke-Python -PythonArgs $installArgs | Out-Null
    $cfgPath = Join-Path $env:USERPROFILE ".codex\config.toml"
    Write-Host "  Codex MCP config updated via installed client: $cfgPath"
}
else {
    Write-Host "[4/4] Skipped Codex config update (Target=$Target)"
}

if ($Target -in @("abacus", "both")) {
    $abacusArgs = @(
        "-m", "ai_mem.mcp_server",
        "--endpoint", $functionEndpoint,
        "--function-key", $defaultFuncKey,
        "--shared-secret", $sharedSecret,
        "--auth-mode", $AuthMode,
        "--user-id", $UserId,
        "--workspace-id", $WorkspaceId,
        "--repo-id", $RepoId
    )
    if (-not [string]::IsNullOrWhiteSpace($EntraScope)) { $abacusArgs += @("--entra-scope", $EntraScope) }
    $abacus = [ordered]@{
        "ai-mem-mcp" = [ordered]@{
            command = "python"
            args = $abacusArgs
        }
    }
    $abacusJson = $abacus | ConvertTo-Json -Depth 8
    $abacusPath = Join-Path $mcpDir "ai-mem-mcp.abacus.json"
    Set-Content -LiteralPath $abacusPath -Value $abacusJson -Encoding utf8
    Write-Host "  Abacus MCP JSON written: $abacusPath"
    Write-Host "  Paste JSON in Abacus Desktop > Settings > MCP settings > Stdio."
}

Write-Host "Client setup complete."
Write-Host "  MCP module : ai_mem.mcp_server"
Write-Host "  Endpoint   : $functionEndpoint"
Write-Host "  UserId     : $UserId"
Write-Host "  Workspace  : $WorkspaceId"
Write-Host "  Repo       : $RepoId"
