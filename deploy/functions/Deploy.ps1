#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ParametersFile,
    [ValidateSet('WhatIf','Apply')][string]$Mode = 'WhatIf',
    [string]$Confirmation,
    [string]$CapacityEvidenceHash,
    [switch]$BootstrapOnly
)
$ErrorActionPreference = 'Stop'
$ParametersFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ParametersFile)
. (Join-Path $PSScriptRoot '..\shared\Azure.ps1')
$arguments = @('deploy','--target','functions','--parameters-file',$ParametersFile,'--mode',$Mode)
if ($Confirmation) { $arguments += @('--confirmation',$Confirmation) }
if ($CapacityEvidenceHash) { $arguments += @('--capacity-evidence-hash',$CapacityEvidenceHash) }
if ($BootstrapOnly) { $arguments += '--bootstrap-only' }
Invoke-AdvisorOperation -Arguments $arguments
