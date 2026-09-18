#requires -Version 7.4
[CmdletBinding()]
param([switch]$CheckPrivateDirectory, [string]$Directory)

$script:AdvisorRepository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$script:AdvisorSubscription = 'adca540e-984c-4cab-bc86-5d38bf979841'

function Assert-AdvisorNoLinks {
    param([Parameter(Mandatory)][string]$Path)
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe linked deployment path.' }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function New-AdvisorPrivateDirectory {
    $root = Join-Path $script:AdvisorRepository 'deploy\out'
    Assert-AdvisorNoLinks $root
    [IO.Directory]::CreateDirectory($root) | Out-Null
    $path = Join-Path $root ('.private-' + [Guid]::NewGuid().ToString('N'))
    if ($IsWindows) {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $security = [Security.AccessControl.DirectorySecurity]::new()
        $security.SetOwner($sid)
        $security.SetAccessRuleProtection($true, $false)
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid, [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $security.AddAccessRule($rule)
        [IO.FileSystemAclExtensions]::Create([IO.DirectoryInfo]::new($path), $security)
    } else {
        [IO.Directory]::CreateDirectory($path, [IO.UnixFileMode]'UserRead, UserWrite, UserExecute') | Out-Null
    }
    Assert-AdvisorPrivateDirectory $path
    return $path
}

function Assert-AdvisorPrivateDirectory {
    param([Parameter(Mandatory)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath((Join-Path $script:AdvisorRepository 'deploy\out'))
    if ([IO.Path]::GetDirectoryName($full) -ne $root -or [IO.Path]::GetFileName($full) -notmatch '^\.private-[a-f0-9]{32}$') {
        throw 'Private directory is outside the owned scope.'
    }
    Assert-AdvisorNoLinks $full
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw 'Private directory is missing.' }
    if ($IsWindows) {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $acl = Get-Acl -LiteralPath $full
        if (-not $acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid) {
            throw 'Private directory owner/inheritance mismatch.'
        }
        $allow = @($acl.Access | Where-Object AccessControlType -eq 'Allow')
        if (-not $allow.Count) { throw 'Private directory access is missing.' }
        foreach ($rule in $allow) {
            if ($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $sid) {
                throw 'Private directory permits another principal.'
            }
        }
    } elseif ([int][IO.File]::GetUnixFileMode($full) -ne 448) {
        throw 'Private directory must have mode 0700.'
    }
}

function Remove-AdvisorPrivateDirectory {
    param([Parameter(Mandatory)][string]$Path)
    Assert-AdvisorPrivateDirectory $Path
    function Remove-OwnedChildren([string]$Parent) {
        foreach ($item in Get-ChildItem -LiteralPath $Parent -Force) {
            Assert-AdvisorNoLinks $item.FullName
            if ($item.PSIsContainer) { Remove-OwnedChildren $item.FullName }
            Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
        }
    }
    Remove-OwnedChildren $Path
    Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
}

function ConvertTo-AdvisorSafeValue {
    param($Value)
    if ($Value -is [Collections.IDictionary]) {
        $safe = @{}
        foreach ($key in $Value.Keys) {
            $safe[$key] = if ($key -match '(?i)secret|password|token|(^|_)keys?($|_)') { '[redacted]' } else { ConvertTo-AdvisorSafeValue $Value[$key] }
        }
        return $safe
    }
    if ($Value -is [array]) { return ,@($Value | ForEach-Object { ConvertTo-AdvisorSafeValue $_ }) }
    return $Value
}

function Invoke-CheckedAz {
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$AllowEmpty)
    $private = New-AdvisorPrivateDirectory
    try {
        $command = if ($env:ADVISOR_AZ_COMMAND) { $env:ADVISOR_AZ_COMMAND } else { (Get-Command az -ErrorAction Stop).Source }
        $errorFile = Join-Path $private 'stderr.txt'
        try {
            $output = @(& $command @Arguments --only-show-errors --output json 2> $errorFile)
            $exitCode = $LASTEXITCODE
        } catch { throw 'Azure CLI could not execute; raw diagnostics suppressed.' }
        if ($exitCode -ne 0) { throw "Azure CLI failed (exit $exitCode); raw diagnostics suppressed." }
        $text = $output -join "`n"
        if (-not $text.Trim()) {
            if ($AllowEmpty) { return $null }
            throw 'Azure CLI returned an empty response.'
        }
        try { $result = ConvertFrom-Json -InputObject $text -AsHashtable -Depth 100 -ErrorAction Stop }
        catch { throw 'Azure CLI returned malformed JSON; raw diagnostics suppressed.' }
        if ($null -eq $result -and -not $AllowEmpty) { throw 'Azure CLI returned a null response.' }
        return ConvertTo-AdvisorSafeValue $result
    } finally {
        Remove-AdvisorPrivateDirectory $private
    }
}

function Get-AdvisorDeploymentArguments {
    param(
        [ValidateSet('aca','functions','bench')][string]$Target,
        [ValidateSet('validate','what-if','create')][string]$Action,
        [string]$TemplateFile, [string]$ParametersFile,
        [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$DeploymentName
    )
    $scope = if ($Target -eq 'bench') { 'group' } else { 'sub' }
    $arguments = @('deployment', $scope, $Action, '--subscription', $script:AdvisorSubscription, '--name', $DeploymentName)
    if ($scope -eq 'sub') { $arguments += @('--location','southeastasia') }
    else {
        $arguments += @('--resource-group','rg-copilot-advisor-aca-exp')
        if ($Action -eq 'create') { $arguments += @('--mode','Incremental') }
    }
    if ($Action -eq 'what-if') { $arguments += @('--no-pretty-print','--result-format','ResourceIdOnly') }
    return $arguments + @('--template-file',$TemplateFile,'--parameters',"@$ParametersFile")
}

function Invoke-AdvisorOperation {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $python = if ($env:ADVISOR_PYTHON_COMMAND) { $env:ADVISOR_PYTHON_COMMAND } elseif ($IsWindows) {
        Join-Path $script:AdvisorRepository '.venv\Scripts\python.exe'
    } else { Join-Path $script:AdvisorRepository '.venv/bin/python' }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Configure the workspace Python environment first.' }
    $private = New-AdvisorPrivateDirectory
    try {
        & $python -m advisor_deploy.operations @Arguments --repository $script:AdvisorRepository --private-directory $private
        if ($LASTEXITCODE -ne 0) { throw 'Deployment guard blocked the operation; inspect the safe structured report.' }
    } finally {
        Remove-AdvisorPrivateDirectory $private
    }
}

if ($CheckPrivateDirectory) {
    Assert-AdvisorPrivateDirectory $Directory
    '{"private_directory_valid":true}'
}
