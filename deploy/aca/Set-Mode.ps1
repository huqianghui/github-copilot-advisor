#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OutputsFile,
    [Parameter(Mandatory)][ValidateSet('Resident','ScaleToZero')][string]$Mode,
    [switch]$Apply,
    [string]$Confirmation
)
$ErrorActionPreference = 'Stop'
$OutputsFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputsFile)
. (Join-Path $PSScriptRoot '..\shared\Azure.ps1')
$operationMode = if ($Apply) { 'Apply' } else { 'WhatIf' }
$arguments = @('set-mode','--outputs-file',$OutputsFile,'--scale',$Mode,'--mode',$operationMode)
if ($Confirmation) { $arguments += @('--confirmation',$Confirmation) }
Invoke-AdvisorOperation -Arguments $arguments
