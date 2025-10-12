param(
    [string]$Name = 'VideoCatalog (A2.0)'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptRoot '..')
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) {
    Write-Error 'Unable to resolve the Desktop folder for the current user.'
    exit 2
}

$target = Join-Path $projectRoot 'scripts/start_videocatalog.bat'
if (-not (Test-Path $target)) {
    Write-Error "Launcher not found at $target"
    exit 3
}

$shortcutPath = Join-Path $desktop ($Name + '.lnk')
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $projectRoot
$iconCandidate = Join-Path $projectRoot 'web/icon.ico'
if (Test-Path $iconCandidate) {
    $shortcut.IconLocation = $iconCandidate
}
$shortcut.Save()

Write-Host "Created shortcut at $shortcutPath" -ForegroundColor Cyan
