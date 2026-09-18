#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ParametersFile,
    [ValidateSet('WhatIf','Apply')][string]$Mode = 'WhatIf',
    [string]$Confirmation
)
$ErrorActionPreference = 'Stop'
$ParametersFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ParametersFile)
. (Join-Path $PSScriptRoot '..\shared\Azure.ps1')
$arguments = @('deploy','--target','aca','--parameters-file',$ParametersFile,'--mode',$Mode)
if ($Confirmation) { $arguments += @('--confirmation',$Confirmation) }
Invoke-AdvisorOperation -Arguments $arguments
