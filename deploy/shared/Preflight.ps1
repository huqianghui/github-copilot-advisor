#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('aca','functions')][string]$Target,
    [Parameter(Mandatory)][string]$ParametersFile,
    [string]$CapacityEvidenceHash
)
$ErrorActionPreference = 'Stop'
$ParametersFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ParametersFile)
. (Join-Path $PSScriptRoot 'Azure.ps1')
$arguments = @('preflight','--target',$Target,'--parameters-file',$ParametersFile)
if ($CapacityEvidenceHash) { $arguments += @('--capacity-evidence-hash',$CapacityEvidenceHash) }
Invoke-AdvisorOperation -Arguments $arguments
