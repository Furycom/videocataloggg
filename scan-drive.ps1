[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Label,
    [Parameter(Mandatory = $true)][string] $MountPath,
    [string] $CatalogDb,
    [string] $ShardDb,
    [switch] $VerboseMode,
    [switch] $DebugMode
)

$ErrorActionPreference = 'Stop'

$scriptRoot = $PSScriptRoot
$pythonExe = Join-Path -Path $scriptRoot -ChildPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = 'python'
}

$scanScript = Join-Path -Path $scriptRoot -ChildPath 'scan_drive.py'
if (-not (Test-Path -LiteralPath $scanScript)) {
    Write-Error "Unable to locate scan_drive.py at $scanScript."
    exit 1
}

$args = @($scanScript, '--label', $Label, '--mount', $MountPath)
if ($CatalogDb) {
    $args += @('--catalog-db', $CatalogDb)
}
if ($ShardDb) {
    $args += @('--shard-db', $ShardDb)
}
if ($VerboseMode.IsPresent) {
    $args += '--verbose'
}
if ($DebugMode.IsPresent) {
    $args += '--debug'
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $pythonExe
foreach ($arg in $args) {
    [void]$psi.ArgumentList.Add($arg)
}
$psi.WorkingDirectory = $scriptRoot
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false

try {
    $process = [System.Diagnostics.Process]::Start($psi)
} catch {
    Write-Error $_
    exit 1
}

$process.WaitForExit()

$stdOut = $process.StandardOutput.ReadToEnd()
if ($stdOut) {
    Write-Output $stdOut
}
$stdErr = $process.StandardError.ReadToEnd()
if ($stdErr) {
    Write-Error $stdErr
}

exit $process.ExitCode
