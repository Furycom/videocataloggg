[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][string]$MountPath,
    [string]$CatalogPath,
    [switch]$DebugSlowEnumeration
)

$ErrorActionPreference = 'Stop'
$scriptRoot = $PSScriptRoot
$python = Join-Path $scriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -Path $python)) {
    $python = 'python'
}

$scanScript = Join-Path $scriptRoot 'scan_drive.py'
if (-not (Test-Path -Path $scanScript)) {
    Write-Error "scan_drive.py not found at $scanScript."
    exit 1
}

try {
    if (-not $CatalogPath) {
        $CatalogPath = & $python -c "from paths import resolve_working_dir, ensure_working_dir_structure, get_catalog_db_path; wd = resolve_working_dir(); ensure_working_dir_structure(wd); print(get_catalog_db_path(wd))"
        $determineExit = $LASTEXITCODE
        if ($determineExit -ne 0 -or [string]::IsNullOrWhiteSpace($CatalogPath)) {
            throw 'Failed to determine default catalog path.'
        }
        $CatalogPath = $CatalogPath.Trim()
    }
} catch {
    Write-Error $_
    exit 1
}

$arguments = @($scanScript, $Label, $MountPath, $CatalogPath)
if ($DebugSlowEnumeration.IsPresent) {
    $arguments += '--debug-slow-enumeration'
}

& $python @arguments
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Error "scan_drive.py exited with code $exitCode."
    exit $exitCode
}

exit 0
