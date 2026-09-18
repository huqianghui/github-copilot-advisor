#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ParametersFile,
    [ValidateSet('WhatIf','Apply')][string]$Mode = 'WhatIf',
    [ValidateSet('Register','Grant')][string]$Stage = 'Register',
    [string]$Confirmation
)
$ErrorActionPreference = 'Stop'
$ParametersFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ParametersFile)
. (Join-Path $PSScriptRoot 'Azure.ps1')
$arguments = @('identity','--parameters-file',$ParametersFile,'--mode',$Mode,'--stage',$Stage)
if ($Confirmation) { $arguments += @('--confirmation',$Confirmation) }
Invoke-AdvisorOperation -Arguments $arguments
