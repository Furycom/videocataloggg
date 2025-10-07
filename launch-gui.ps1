[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$scriptRoot = $PSScriptRoot
$python = Join-Path $scriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -Path $python)) {
    $python = 'python'
}

$guiScript = Join-Path $scriptRoot 'DiskScannerGUI.py'

try {
    & $python $guiScript
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        Write-Error "Disk Scanner GUI exited with code $exitCode."
        exit 1
    }
} catch {
    Write-Error $_
    exit 1
}
