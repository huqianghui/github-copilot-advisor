[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('aca', 'functions')][string]$Target,
    [Parameter(Mandatory)][string]$Output,
    [Parameter(Mandatory)][string]$EscalationConfig,
    [Parameter(Mandatory)][string]$DiagnosticsConfig,
    [string]$BuildImage
)

$ErrorActionPreference = 'Stop'
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
$EscalationConfig = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EscalationConfig)
$DiagnosticsConfig = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DiagnosticsConfig)
$packageArguments = @(
    'run', '--locked', '--no-sync', '--package', 'advisor-deploy',
    'python', '-m', 'advisor_deploy', 'package',
    '--target', $Target, '--output', $Output,
    '--escalation-config', $EscalationConfig, '--diagnostics-config', $DiagnosticsConfig
)
if ($BuildImage) {
    if ($Target -ne 'aca') { throw 'Local image building is ACA-only.' }
    $packageArguments += @('--build-image', $BuildImage)
}
Push-Location $repository
try {
    & uv @packageArguments
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed; no deployment was attempted.' }
} finally {
    Pop-Location
}
