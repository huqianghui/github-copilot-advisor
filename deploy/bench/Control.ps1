#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ParametersFile,
    [ValidateSet('start','status')][string]$Action = 'status',
    [ValidateSet('WhatIf','Apply')][string]$Mode = 'WhatIf',
    [string]$Confirmation
)
$ErrorActionPreference = 'Stop'
$ParametersFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ParametersFile)
. (Join-Path $PSScriptRoot '..\shared\Azure.ps1')
$arguments = @('bench-control','--parameters-file',$ParametersFile,'--bench-action',$Action,'--mode',$Mode)
if ($Confirmation) { $arguments += @('--confirmation',$Confirmation) }
Invoke-AdvisorOperation -Arguments $arguments
