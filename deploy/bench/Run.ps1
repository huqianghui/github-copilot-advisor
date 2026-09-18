[CmdletBinding()]
param(
    [string]$Plan = (Join-Path $PSScriptRoot 'run.example.json'),
    [string]$Output = (Join-Path $PSScriptRoot '..\out\bench'),
    [switch]$AllowCloud
)
$ErrorActionPreference = 'Stop'
$planPath = (Resolve-Path -LiteralPath $Plan).Path
$outputPath = [System.IO.Path]::GetFullPath($Output)
$settings = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
if (-not $settings.local_test -and -not $AllowCloud) {
    throw 'Cloud execution requires -AllowCloud, verified deployment bounds and Entra authorization.'
}
$arguments = @('run', '--no-sync', '--package', 'advisor-deploy', 'python', '-m',
    'advisor_deploy.bench', 'run', '--plan', $planPath, '--output', $outputPath)
if ($AllowCloud) { $arguments += '--allow-cloud' }
Push-Location (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
try {
    & uv @arguments
    if ($LASTEXITCODE -ne 0) { throw "Benchmark stopped with exit code $LASTEXITCODE." }
}
finally {
    Pop-Location
}
