param(
    [string]$Image = "aka-no-claw:rpi5-smoke",
    [string]$Platform = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$WorkspaceRoot = Resolve-Path (Join-Path $RepoRoot "..")

function Invoke-DockerChecked {
    param([string[]]$DockerArgs)
    docker @DockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($DockerArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$buildArgs = @(
    "build",
    "--file", (Join-Path $RepoRoot "docker/rpi5-smoke/Dockerfile"),
    "--tag", $Image
)
if ($Platform) {
    $buildArgs += @("--platform", $Platform)
}
$buildArgs += @($RepoRoot.Path)

Invoke-DockerChecked $buildArgs

$dockerArgs = @("run", "--rm")
if ($Platform) {
    $dockerArgs += @("--platform", $Platform)
}
$dockerArgs += @(
    "--volume", "$($WorkspaceRoot.Path):/source:ro",
    $Image
)

Invoke-DockerChecked $dockerArgs
