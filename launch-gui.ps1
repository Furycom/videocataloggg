[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$scriptRoot = $PSScriptRoot
$pythonExe = Join-Path -Path $scriptRoot -ChildPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = 'python'
}

$guiScript = Join-Path -Path $scriptRoot -ChildPath 'DiskScannerGUI.py'
if (-not (Test-Path -LiteralPath $guiScript)) {
    Write-Error "Unable to locate DiskScannerGUI.py at $guiScript."
    exit 1
}

try {
    & $pythonExe $guiScript
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        Write-Error "DiskScannerGUI exited with code $exitCode."
        exit 1
    }
} catch {
    Write-Error $_
    exit 1
}

exit 0
