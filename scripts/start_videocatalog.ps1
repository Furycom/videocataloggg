param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Info {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Cyan
}

function Write-Warn {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptRoot '..')
Set-Location $projectRoot

Write-Info "VideoCatalog A2.0 launcher"

if (-not $env:USERPROFILE) {
    Write-Err "USERPROFILE environment variable is not set."
    exit 2
}

$workingDir = Join-Path $env:USERPROFILE 'VideoCatalog'
$directories = @(
    $workingDir,
    (Join-Path $workingDir 'data'),
    (Join-Path $workingDir 'logs'),
    (Join-Path $workingDir 'exports'),
    (Join-Path $workingDir 'backups'),
    (Join-Path $workingDir 'vectors')
)
foreach ($dir in $directories) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
}

$settingsSource = Join-Path $projectRoot 'settings.json'
$userSettingsPath = Join-Path $workingDir 'settings.json'
if (-not (Test-Path $userSettingsPath)) {
    if (Test-Path $settingsSource) {
        Copy-Item -Path $settingsSource -Destination $userSettingsPath -Force
    } else {
        '{}' | Set-Content -Path $userSettingsPath -Encoding UTF8
    }
}

try {
    $settingsRaw = Get-Content -Path $userSettingsPath -Raw -Encoding UTF8
} catch {
    $settingsRaw = '{}'
}

try {
    $settings = $settingsRaw | ConvertFrom-Json
} catch {
    Write-Warn "settings.json is invalid JSON. Recreating defaults."
    $settings = New-Object PSObject
}

if (-not ($settings | Get-Member -Name 'server' -ErrorAction SilentlyContinue)) {
    $settings | Add-Member -NotePropertyName 'server' -NotePropertyValue ([pscustomobject]@{})
}
$serverSettings = $settings.server
if ($null -eq $serverSettings -or -not ($serverSettings -is [psobject])) {
    $serverSettings = [pscustomobject]@{}
    $settings.server = $serverSettings
}
$serverSettings.host = '127.0.0.1'
$serverSettings.port = 27182
$serverSettings.lan_refuse = $true

if (-not ($settings | Get-Member -Name 'catalog_db' -ErrorAction SilentlyContinue)) {
    $settings | Add-Member -NotePropertyName 'catalog_db' -NotePropertyValue ''
}
$catalogDbPath = Join-Path $workingDir 'catalog.db'
$targetCatalog = [System.IO.Path]::GetFullPath($catalogDbPath)
$needsCatalogUpdate = $true
$existingCatalog = [string]$settings.catalog_db
if (-not [string]::IsNullOrWhiteSpace($existingCatalog)) {
    $expandedCatalog = [Environment]::ExpandEnvironmentVariables($existingCatalog)
    try {
        $resolvedExisting = [System.IO.Path]::GetFullPath($expandedCatalog)
    } catch {
        $resolvedExisting = $expandedCatalog
    }
    if ($resolvedExisting.Trim().ToLowerInvariant() -eq $targetCatalog.Trim().ToLowerInvariant()) {
        $needsCatalogUpdate = $false
    }
}
if ($needsCatalogUpdate) {
    $settings.catalog_db = $targetCatalog
}

if (-not ($settings | Get-Member -Name 'working_dir' -ErrorAction SilentlyContinue)) {
    $settings | Add-Member -NotePropertyName 'working_dir' -NotePropertyValue $workingDir
} else {
    $settings.working_dir = $workingDir
}

$updatedJson = $settings | ConvertTo-Json -Depth 100
[System.IO.File]::WriteAllText($userSettingsPath, $updatedJson + [Environment]::NewLine, [System.Text.Encoding]::UTF8)

foreach ($suffix in @('catalog.db-wal', 'catalog.db-shm')) {
    $path = Join-Path $projectRoot $suffix
    if (Test-Path $path) {
        Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
    }
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Err 'Python executable not found on PATH.'
    exit 3
}
$pythonExe = $pythonCmd.Source
if (-not $pythonExe) { $pythonExe = $pythonCmd.Path }
if (-not $pythonExe) { $pythonExe = $pythonCmd.Definition }

$pipCmd = Get-Command pip -ErrorAction SilentlyContinue
if ($pipCmd) {
    $pipExe = $pipCmd.Source
    if (-not $pipExe) { $pipExe = $pipCmd.Path }
    if (-not $pipExe) { $pipExe = $pipCmd.Definition }
} else {
    Write-Warn 'pip executable not found on PATH. Falling back to python -m pip.'
    $pipExe = $null
}

$env:VIDEOCATALOG_HOME = $workingDir

$uvicornCheck = & $pythonExe -c "import uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    $requirementsPath = Join-Path $projectRoot 'requirements-windows.txt'
    if (-not (Test-Path $requirementsPath)) {
        $requirementsPath = Join-Path $projectRoot 'requirements.txt'
    }
    Write-Info "Installing Python dependencies from $requirementsPath"
    if ($pipExe) {
        & $pipExe install -r $requirementsPath
        if ($LASTEXITCODE -ne 0) {
            Write-Err 'pip install failed.'
            exit 4
        }
    } else {
        & $pythonExe -m pip install -r $requirementsPath
        if ($LASTEXITCODE -ne 0) {
            Write-Err 'python -m pip install failed.'
            exit 4
        }
    }
}

if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Warn 'ffprobe not found on PATH. Quality headers will be disabled until installed.'
}

$tempProbe = [System.IO.Path]::GetTempFileName()
$probeScript = @"
from pathlib import Path
import json
from orchestrator.gpu import GPUManager
from orchestrator.logs import OrchestratorLogger
from core.settings import load_settings
from core.paths import ensure_working_dir_structure

working_dir = Path(r'$workingDir')
ensure_working_dir_structure(working_dir)
settings = load_settings(working_dir)
orch_cfg = dict(settings.get('orchestrator', {}))
gpu_cfg = dict(settings.get('gpu', {}))
manager = GPUManager(
    logger=OrchestratorLogger(working_dir),
    safety_margin_mb=int(orch_cfg.get('gpu', {}).get('safety_margin_mb', 1024) if isinstance(orch_cfg.get('gpu'), dict) else 1024),
    lease_ttl_s=int(orch_cfg.get('lease_ttl_s', 120)),
)
required = int(gpu_cfg.get('min_free_vram_mb', 512))
result = manager.preflight(required)
info = result.info
payload = {
    'ok': bool(result.ok),
    'present': bool(info.present),
    'name': info.name,
    'free_mb': int(getattr(info, 'vram_free_mb', 0) or 0),
    'total_mb': int(getattr(info, 'vram_total_mb', 0) or 0),
    'reason': result.reason or info.error,
}
print(json.dumps(payload))
"@
[System.IO.File]::WriteAllText($tempProbe, $probeScript, [System.Text.Encoding]::UTF8)
$gpuProbeOutput = & $pythonExe $tempProbe
$gpuStatus = $null
if ($LASTEXITCODE -eq 0 -and $gpuProbeOutput) {
    try {
        $gpuStatus = $gpuProbeOutput | ConvertFrom-Json
    } catch {
        $gpuStatus = $null
    }
}
Remove-Item -Path $tempProbe -Force -ErrorAction SilentlyContinue

if ($gpuStatus -and $gpuStatus.ok) {
    $gpuSummary = "ready ($($gpuStatus.name) - free $($gpuStatus.free_mb) MB of $($gpuStatus.total_mb) MB)"
} elseif ($gpuStatus -and $gpuStatus.present) {
    $gpuSummary = "not-ready (GPU present but gated: $($gpuStatus.reason))"
} elseif ($gpuStatus) {
    $gpuSummary = "not-ready ($($gpuStatus.reason))"
} else {
    $gpuSummary = 'not-ready (probe failed)'
}

Write-Info "GPU status: $gpuSummary"

function New-Process {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Name,
        [System.Collections.Generic.Dictionary[string, object]]$Handlers,
        [System.Threading.ManualResetEvent]$ReadyEvent = $null,
        [string[]]$ReadyPatterns = $null
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ($Arguments | ForEach-Object {
        if ($_ -match '[\s\"]') {
            '"' + ($_ -replace '"', '\"') + '"'
        } else {
            $_
        }
    }) -join ' '
    $psi.WorkingDirectory = $projectRoot
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables['VIDEOCATALOG_HOME'] = $workingDir
    $psi.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    $process.EnableRaisingEvents = $true
    if (-not $process.Start()) {
        throw "Failed to start $Name process."
    }

    $stdoutHandler = [System.Diagnostics.DataReceivedEventHandler]{
        param($sender, $args)
        if ([string]::IsNullOrWhiteSpace($args.Data)) { return }
        Write-Host "[$Name] $($args.Data)"
        if ($ReadyEvent -and $ReadyPatterns) {
            foreach ($pattern in $ReadyPatterns) {
                if ($args.Data -like $pattern) {
                    $ReadyEvent.Set() | Out-Null
                    break
                }
            }
        }
    }
    $stderrHandler = [System.Diagnostics.DataReceivedEventHandler]{
        param($sender, $args)
        if ([string]::IsNullOrWhiteSpace($args.Data)) { return }
        Write-Host "[$Name] $($args.Data)" -ForegroundColor DarkYellow
    }

    $process.add_OutputDataReceived($stdoutHandler)
    $process.add_ErrorDataReceived($stderrHandler)
    $process.BeginOutputReadLine()
    $process.BeginErrorReadLine()

    $Handlers[$Name] = @{ Output = $stdoutHandler; Error = $stderrHandler; Process = $process }
    return $process
}

$handlers = New-Object 'System.Collections.Generic.Dictionary[string, object]'
$orchReady = New-Object System.Threading.ManualResetEvent($false)
$orchProcess = New-Process -FilePath $pythonExe -Arguments @('-m', 'orchestrator.scheduler', '--working-dir', $workingDir) -Name 'orchestrator' -Handlers $handlers -ReadyEvent $orchReady -ReadyPatterns @('ORCH_HEARTBEAT ready*')

if (-not $orchReady.WaitOne(20000)) {
    if ($orchProcess.HasExited) {
        Write-Err "Orchestrator exited early with code $($orchProcess.ExitCode)."
    } else {
        Write-Err 'Orchestrator failed to report ready within 20 seconds.'
        $orchProcess.Kill()
    }
    exit 5
}

Write-Info "Orchestrator running (PID $($orchProcess.Id))"

$webReady = New-Object System.Threading.ManualResetEvent($false)
$webProcess = New-Process -FilePath $pythonExe -Arguments @('-m', 'videocatalog_api', '--host', '127.0.0.1', '--port', '27182') -Name 'web' -Handlers $handlers -ReadyEvent $webReady -ReadyPatterns @('*Application startup complete*', '*Uvicorn running on http://127.0.0.1:27182*', '*API listening on http://127.0.0.1:27182*')

if (-not $webReady.WaitOne(25000)) {
    if ($webProcess.HasExited) {
        Write-Err "Web server exited early with code $($webProcess.ExitCode)."
    } else {
        Write-Err 'Web server failed to start within 25 seconds.'
        $webProcess.Kill()
    }
    if (-not $orchProcess.HasExited) { $orchProcess.Kill() }
    exit 6
}

Write-Info "Web UI available at http://127.0.0.1:27182"

if (-not $NoBrowser) {
    try {
        Start-Process 'http://127.0.0.1:27182' | Out-Null
    } catch {
        Write-Warn 'Failed to launch browser automatically.'
    }
}

Write-Host ''
Write-Info 'Summary:'
Write-Host "  GPU: $gpuSummary"
Write-Host "  Orchestrator: running (PID $($orchProcess.Id))"
Write-Host '  Web: http://127.0.0.1:27182 (WS/SSE enabled)'
Write-Host ''
Write-Host 'Press Ctrl+C to stop both services.'

$exitCode = 0
try {
    while ($true) {
        if ($orchProcess.HasExited) {
            $exitCode = $orchProcess.ExitCode
            Write-Err "Orchestrator exited with code $exitCode"
            break
        }
        if ($webProcess.HasExited) {
            $exitCode = $webProcess.ExitCode
            Write-Err "Web server exited with code $exitCode"
            break
        }
        Start-Sleep -Seconds 1
    }
} finally {
    foreach ($entry in $handlers.Values) {
        $proc = $entry['Process']
        if ($proc -and -not $proc.HasExited) {
            try { $proc.Kill() } catch {}
        }
        $outputHandler = $entry['Output']
        if ($proc) { $proc.remove_OutputDataReceived($outputHandler) }
        $errorHandler = $entry['Error']
        if ($proc) { $proc.remove_ErrorDataReceived($errorHandler) }
    }
}

exit $exitCode
