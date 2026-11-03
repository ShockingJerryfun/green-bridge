param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$ConfigPath
)

$ErrorActionPreference = 'Stop'
$bridge = Join-Path $PSScriptRoot 'green_bridge.py'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'Python executable was not found.'
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw 'Green service configuration was not found.'
}
& $PythonExe $bridge --config $ConfigPath serve
exit $LASTEXITCODE
