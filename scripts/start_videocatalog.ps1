param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO ] $Message" -ForegroundColor Cyan
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN ] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

function Test-PythonModule {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$ModuleName
    )

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonExe -c "import $ModuleName" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $scriptRoot '..')
Set-Location $root

if (-not $env:USERPROFILE) {
    Write-Err 'USERPROFILE environment variable is not set.'
    exit 2
}

$work = Join-Path $env:USERPROFILE 'VideoCatalog'
$logs = Join-Path $work 'logs'
$directories = @(
    $work,
    (Join-Path $work 'data'),
    $logs,
    (Join-Path $work 'exports'),
    (Join-Path $work 'backups'),
    (Join-Path $work 'vectors')
)
foreach ($dir in $directories) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

$launcherStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$LauncherLog = Join-Path $logs "launcher_$launcherStamp.log"
try {
    Start-Transcript -Path $LauncherLog -Append | Out-Null
} catch {
    Write-Warn "Failed to start transcript at ${LauncherLog}: $($_.Exception.Message)"
}

foreach ($bootstrap in 'start_videocatalog.bat', 'start_videocatalog.ps1') {
    $bootstrapPath = Join-Path $scriptRoot $bootstrap
    if (Test-Path $bootstrapPath) {
        try {
            Unblock-File -Path $bootstrapPath -ErrorAction Stop
        } catch {
            Write-Warn "Unable to unblock ${bootstrapPath}: $($_.Exception.Message)"
        }
    }
}

Write-Info 'VideoCatalog A2.0 launcher'

$orchProcess = $null
$webProcess = $null
$exitCode = 0

try {
    $workFull = [System.IO.Path]::GetFullPath($work)
    $settingsSource = Join-Path $root 'settings.json'
    $userSettingsPath = Join-Path $work 'settings.json'
    if (-not (Test-Path $userSettingsPath)) {
        if (Test-Path $settingsSource) {
            Copy-Item -Path $settingsSource -Destination $userSettingsPath -Force
            Write-Info "Copied default settings to $userSettingsPath"
        } else {
            '{}' | Set-Content -Path $userSettingsPath -Encoding UTF8
            Write-Warn 'Default settings template missing. Created empty settings.json.'
        }
    }

    try {
        $settingsRaw = Get-Content -Path $userSettingsPath -Raw -Encoding UTF8
    } catch {
        Write-Warn "Unable to read $userSettingsPath, using defaults."
        $settingsRaw = '{}'
    }

    try {
        $convertFromJsonParameters = @{ InputObject = $settingsRaw }
        if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('Depth')) {
            $convertFromJsonParameters['Depth'] = 100
        }
        $settings = ConvertFrom-Json @convertFromJsonParameters
    } catch {
        Write-Warn 'settings.json is invalid JSON. Recreating defaults.'
        $settings = [pscustomobject]@{}
    }

    if (-not $settings) {
        $settings = [pscustomobject]@{}
    }

    if (-not ($settings | Get-Member -Name 'server' -ErrorAction SilentlyContinue)) {
        $settings | Add-Member -NotePropertyName 'server' -NotePropertyValue ([pscustomobject]@{})
    }
    $settings.server = if ($settings.server -is [psobject]) { $settings.server } else { [pscustomobject]@{} }
    if ($settings.server.host -ne '127.0.0.1') {
        Write-Warn "server.host set to '$($settings.server.host)'; forcing to 127.0.0.1"
    }
    $settings.server.host = '127.0.0.1'
    if (-not $settings.server.port) { $settings.server | Add-Member -NotePropertyName 'port' -NotePropertyValue 27182 -Force }
    $settings.server.port = 27182
    if ($settings.server.lan_refuse -ne $true) {
        Write-Warn 'server.lan_refuse forced to true to prevent LAN exposure.'
    }
    $settings.server.lan_refuse = $true

    if (-not ($settings | Get-Member -Name 'catalog_db' -ErrorAction SilentlyContinue)) {
        $settings | Add-Member -NotePropertyName 'catalog_db' -NotePropertyValue ''
    }
    $catalogTarget = Join-Path $workFull 'catalog.db'
    $existingCatalog = [string]$settings.catalog_db
    $resolvedCatalog = $catalogTarget
    if (-not [string]::IsNullOrWhiteSpace($existingCatalog)) {
        $expandedCatalog = [Environment]::ExpandEnvironmentVariables($existingCatalog)
        try {
            $resolvedCatalog = [System.IO.Path]::GetFullPath($expandedCatalog)
        } catch {
            Write-Warn "Unable to resolve catalog_db '$existingCatalog', defaulting to $catalogTarget"
            $resolvedCatalog = $catalogTarget
        }
    }
    if (-not ($resolvedCatalog.StartsWith($workFull, [System.StringComparison]::OrdinalIgnoreCase))) {
        Write-Warn "catalog_db path '$resolvedCatalog' is outside $workFull. Resetting."
        $resolvedCatalog = $catalogTarget
    }
    $settings.catalog_db = $resolvedCatalog

    if (-not ($settings | Get-Member -Name 'working_dir' -ErrorAction SilentlyContinue)) {
        $settings | Add-Member -NotePropertyName 'working_dir' -NotePropertyValue $workFull
    }
    $settings.working_dir = $workFull

    $updatedJson = $settings | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($userSettingsPath, $updatedJson + [Environment]::NewLine, [System.Text.Encoding]::UTF8)
    Write-Info "Settings written to $userSettingsPath"

    foreach ($suffix in @('catalog.db-wal', 'catalog.db-shm')) {
        $path = Join-Path $root $suffix
        if (Test-Path $path) {
            Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
            Write-Warn "Removed stray $suffix from repository root."
        }
    }

    $cutoff = (Get-Date).AddDays(-14)
    Get-ChildItem -Path $logs -Filter '*.log' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        ForEach-Object {
            Write-Info "Rotating old launcher log $($_.FullName)"
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
        }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        throw 'Python executable not found on PATH.'
    }
    $pythonExe = $pythonCmd.Source
    if (-not $pythonExe) { $pythonExe = $pythonCmd.Path }
    if (-not $pythonExe) { $pythonExe = $pythonCmd.Definition }
    Write-Info "Python detected at $pythonExe"

    $pipCmd = Get-Command pip -ErrorAction SilentlyContinue
    if ($pipCmd) {
        $pipExe = $pipCmd.Source
        if (-not $pipExe) { $pipExe = $pipCmd.Path }
        if (-not $pipExe) { $pipExe = $pipCmd.Definition }
        Write-Info "pip detected at $pipExe"
    } else {
        Write-Warn 'pip executable not found on PATH. Falling back to python -m pip.'
        $pipExe = $null
    }

    $env:VIDEOCATALOG_HOME = $workFull
    $env:PYTHONUNBUFFERED = '1'

    $uvicornAvailable = Test-PythonModule -PythonExe $pythonExe -ModuleName 'uvicorn'
    if (-not $uvicornAvailable) {
        $requirementsPath = Join-Path $root 'requirements-windows.txt'
        if (-not (Test-Path $requirementsPath)) {
            $requirementsPath = Join-Path $root 'requirements.txt'
        }
        Write-Warn 'uvicorn not found, installing required Python packages.'
        Write-Info "Installing dependencies from $requirementsPath"
        if ($pipExe) {
            & $pipExe install -r $requirementsPath
        } else {
            & $pythonExe -m pip install -r $requirementsPath
        }
        if ($LASTEXITCODE -ne 0) {
            throw 'Dependency installation failed.'
        }
        if (-not (Test-PythonModule -PythonExe $pythonExe -ModuleName 'uvicorn')) {
            throw 'uvicorn is still unavailable after installation.'
        }
    } else {
        Write-Info 'uvicorn module available.'
    }

    if (Get-Command ffprobe -ErrorAction SilentlyContinue) {
        Write-Info 'ffprobe detected on PATH.'
    } else {
        Write-Warn 'ffprobe not found on PATH. Media inspection will be degraded.'
    }

    $gpuSummary = 'not probed'
    $tempProbe = $null
    try {
        $tempProbe = [System.IO.Path]::GetTempFileName()
        $probeScript = @"
from pathlib import Path
import json
from orchestrator.gpu import GPUManager
from orchestrator.logs import OrchestratorLogger
from core.settings import load_settings
from core.paths import ensure_working_dir_structure

working_dir = Path(r'$workFull')
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
        if ($LASTEXITCODE -eq 0 -and $gpuProbeOutput) {
            try {
                $gpuStatus = $gpuProbeOutput | ConvertFrom-Json
            } catch {
                $gpuStatus = $null
            }
            if ($gpuStatus -and $gpuStatus.ok) {
                $gpuSummary = "ready ($($gpuStatus.name) - free $($gpuStatus.free_mb) MB of $($gpuStatus.total_mb) MB)"
            } elseif ($gpuStatus -and $gpuStatus.present) {
                $gpuSummary = "not-ready (present but gated: $($gpuStatus.reason))"
            } elseif ($gpuStatus) {
                $gpuSummary = "not-ready ($($gpuStatus.reason))"
            } else {
                $gpuSummary = 'not-ready (probe parse failed)'
            }
        } else {
            $gpuSummary = 'not-ready (probe failed)'
        }
    } finally {
        if ($tempProbe -and (Test-Path $tempProbe)) {
            Remove-Item -Path $tempProbe -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Info "GPU status: $gpuSummary"

    $orchLogStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $OrchLog = Join-Path $logs "orchestrator_$orchLogStamp.log"
    $WebLog = Join-Path $logs "web_$orchLogStamp.log"

    $orchProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'orchestrator.scheduler', '--working-dir', $workFull) -WorkingDirectory $root -RedirectStandardOutput $OrchLog -RedirectStandardError $OrchLog -NoNewWindow -PassThru
    Write-Info "Orchestrator starting (PID $($orchProcess.Id)) logging to $OrchLog"

    $orchReady = $false
    $orchStart = Get-Date
    while (-not $orchReady -and -not $orchProcess.HasExited) {
        if ((Get-Date) - $orchStart -gt [TimeSpan]::FromSeconds(20)) {
            break
        }
        if (Test-Path $OrchLog) {
            $recent = Get-Content -Path $OrchLog -Tail 40 -ErrorAction SilentlyContinue
            if ($recent -and ($recent -match 'ORCH_HEARTBEAT')) {
                $orchReady = $true
                break
            }
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $orchReady) {
        if ($orchProcess.HasExited) {
            throw "Orchestrator exited early with code $($orchProcess.ExitCode)."
        }
        throw 'Orchestrator failed to emit heartbeat within 20 seconds.'
    }

    Write-Info 'Orchestrator heartbeat detected.'

    $webProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'videocatalog_api', '--host', '127.0.0.1', '--port', '27182') -WorkingDirectory $root -RedirectStandardOutput $WebLog -RedirectStandardError $WebLog -NoNewWindow -PassThru
    Write-Info "Web server starting (PID $($webProcess.Id)) logging to $WebLog"

    $webReady = $false
    $webStart = Get-Date
    while (-not $webReady -and -not $webProcess.HasExited) {
        if ((Get-Date) - $webStart -gt [TimeSpan]::FromSeconds(25)) {
            break
        }
        if (Test-Path $WebLog) {
            $recentWeb = Get-Content -Path $WebLog -Tail 40 -ErrorAction SilentlyContinue
            if ($recentWeb -and ($recentWeb -match 'Application startup complete' -or $recentWeb -match 'Uvicorn running on http://127.0.0.1:27182')) {
                $webReady = $true
                break
            }
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $webReady) {
        if ($webProcess.HasExited) {
            throw "Web server exited early with code $($webProcess.ExitCode)."
        }
        throw 'Web server failed to report ready within 25 seconds.'
    }

    Write-Info 'Web server reported ready. Performing readiness check...'
    Start-Sleep -Seconds 2

    $webUri = 'http://127.0.0.1:27182'
    if (-not $NoBrowser) {
        try {
            Start-Process $webUri | Out-Null
            Write-Info 'Opened default browser.'
        } catch {
            Write-Warn 'Failed to launch browser automatically.'
        }
    }

    Write-Info 'Services running. Press Ctrl+C to stop.'
    Write-Info "  GPU: $gpuSummary"
    Write-Info "  Orchestrator log: $OrchLog"
    Write-Info "  Web log: $WebLog"

    while ($true) {
        if ($orchProcess.HasExited) {
            $exitCode = if ($orchProcess.ExitCode -ne 0) { $orchProcess.ExitCode } else { 0 }
            Write-Err "Orchestrator exited with code $($orchProcess.ExitCode)"
            break
        }
        if ($webProcess.HasExited) {
            $exitCode = if ($webProcess.ExitCode -ne 0) { $webProcess.ExitCode } else { 0 }
            Write-Err "Web server exited with code $($webProcess.ExitCode)"
            break
        }
        Start-Sleep -Seconds 1
    }

} catch {
    Write-Err $_.Exception.Message
    Write-Err "Launcher log captured at $LauncherLog"
    if ($exitCode -eq 0) { $exitCode = 1 }
} finally {
    foreach ($proc in @($webProcess, $orchProcess)) {
        if ($proc -and -not $proc.HasExited) {
            try {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            } catch {
                Write-Warn "Failed to stop process $($proc.Id): $($_.Exception.Message)"
            }
        }
    }
    try {
        Stop-Transcript | Out-Null
    } catch {
        # ignore transcript stop failures
    }
}

exit $exitCode
