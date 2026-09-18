#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OutputsFile,
    [ValidateSet('WhatIf','Apply')][string]$Mode = 'WhatIf',
    [switch]$WhatIf,
    [string]$Confirmation,
    [switch]$TrafficStopped,
    [ValidateSet('drain','abandon')][string]$InFlightDecision
)
$ErrorActionPreference = 'Stop'
$OutputsFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputsFile)
. (Join-Path $PSScriptRoot 'shared\Azure.ps1')
if ($WhatIf) { $Mode = 'WhatIf' }
$arguments = @('cleanup','--outputs-file',$OutputsFile,'--mode',$Mode)
if ($Confirmation) { $arguments += @('--confirmation',$Confirmation) }
if ($TrafficStopped) { $arguments += '--traffic-stopped' }
if ($InFlightDecision) { $arguments += @('--in-flight',$InFlightDecision) }
Invoke-AdvisorOperation -Arguments $arguments
