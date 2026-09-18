#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OutputsFile,
    [string]$DeploymentName,
    [ValidateSet('WhatIf','Apply')][string]$Mode = 'WhatIf',
    [string]$Confirmation
)
$ErrorActionPreference = 'Stop'
$OutputsFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputsFile)
. (Join-Path $PSScriptRoot 'Azure.ps1')
$arguments = @('reconcile','--outputs-file',$OutputsFile,'--mode',$Mode)
if ($DeploymentName) { $arguments += @('--deployment-name',$DeploymentName) }
if ($Confirmation) { $arguments += @('--confirmation',$Confirmation) }
Invoke-AdvisorOperation -Arguments $arguments
