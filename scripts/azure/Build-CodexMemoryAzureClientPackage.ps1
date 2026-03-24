[CmdletBinding()]
param(
    [string]$OutputDirectory = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Python {
    param([string[]]$PythonArgs)
    & python @PythonArgs
    if ($LASTEXITCODE -ne 0) { throw "python failed: python $($PythonArgs -join ' ')" }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$packageRoot = Join-Path $repoRoot "client"
if (-not (Test-Path $packageRoot)) { throw "Client package not found: $packageRoot" }

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $packageRoot "dist"
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

Write-Host "[1/2] Ensure Python build tooling"
Invoke-Python -PythonArgs @("-m", "pip", "install", "--user", "--upgrade", "build")

Write-Host "[2/2] Build ai-mem client artifacts"
Invoke-Python -PythonArgs @("-m", "build", "--outdir", $OutputDirectory, $packageRoot)

Write-Host "Artifacts written to: $OutputDirectory"
