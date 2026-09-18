[CmdletBinding()]
param(
    [string]$Plan = (Join-Path $PSScriptRoot 'run.example.json'),
    [string]$Output = (Join-Path $PSScriptRoot '..\out\bench-context'),
    [string]$ImageTag,
    [string]$BaseImage,
    [string]$DockerContext
)
$ErrorActionPreference = 'Stop'
if (($BaseImage -or $DockerContext) -and -not $ImageTag) {
    throw '-BaseImage and -DockerContext require -ImageTag.'
}
$planPath = (Resolve-Path -LiteralPath $Plan).Path
$outputPath = [System.IO.Path]::GetFullPath($Output)
Push-Location (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
try {
    & uv run --no-sync --package advisor-deploy python -m advisor_deploy.bench.build context `
        --plan $planPath --output $outputPath
    if ($LASTEXITCODE -ne 0) { throw 'Locked benchmark context preparation failed.' }
    if ($ImageTag) {
        $arguments = @('run', '--no-sync', '--package', 'advisor-deploy', 'python', '-m',
            'advisor_deploy.bench.build', 'image', $outputPath, '--tag', $ImageTag)
        if ($BaseImage) { $arguments += @('--base-image', $BaseImage) }
        if ($DockerContext) { $arguments += @('--docker-context', $DockerContext) }
        & uv @arguments
        if ($LASTEXITCODE -ne 0) { throw 'Benchmark image build or offline Linux import check failed.' }
    }
}
finally {
    Pop-Location
}
