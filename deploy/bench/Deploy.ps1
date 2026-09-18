#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ParametersFile,
    [ValidateSet('Bootstrap','Publish','App')][string]$Stage = 'Bootstrap',
    [ValidateSet('WhatIf','Apply')][string]$Mode = 'WhatIf',
    [string]$Confirmation
)
$ErrorActionPreference = 'Stop'
$ParametersFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ParametersFile)
. (Join-Path $PSScriptRoot '..\shared\Azure.ps1')
$arguments = @('bench-deploy','--parameters-file',$ParametersFile,'--bench-stage',$Stage,'--mode',$Mode)
if ($Confirmation) { $arguments += @('--confirmation',$Confirmation) }
Invoke-AdvisorOperation -Arguments $arguments
