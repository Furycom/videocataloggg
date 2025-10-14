param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

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

function Invoke-PipInstall {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [string]$PipExe,
        [string]$LogFile
    )

    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $effectiveArgs = $Arguments
    if (-not ($effectiveArgs -contains '--disable-pip-version-check')) {
        $effectiveArgs = @('--disable-pip-version-check') + $effectiveArgs
    }

    $commandPath = $null
    $commandArgs = @()
    if ($PipExe) {
        $commandPath = $PipExe
        $commandArgs = $effectiveArgs
    } else {
        $commandPath = $PythonExe
        $commandArgs = @('-m', 'pip') + $effectiveArgs
    }

    $exitCode = -1

    if ($LogFile) {
        $logDirectory = Split-Path -Parent $LogFile
        if ($logDirectory -and -not (Test-Path $logDirectory)) {
            New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        }

        $timestamp = Get-Date -Format o
        $commandLine = "`"$commandPath`" $($commandArgs -join ' ')"
        Add-Content -Path $LogFile -Value "[$timestamp] COMMAND: $commandLine" -Encoding UTF8

        $stdoutTemp = Join-Path $logDirectory ("pip_$([guid]::NewGuid().ToString('N'))_stdout.log")
        $stderrTemp = Join-Path $logDirectory ("pip_$([guid]::NewGuid().ToString('N'))_stderr.log")

        try {
            $process = Start-Process -FilePath $commandPath -ArgumentList $commandArgs -RedirectStandardOutput $stdoutTemp -RedirectStandardError $stderrTemp -NoNewWindow -Wait -PassThru
            $exitCode = if ($process) { $process.ExitCode } else { $LASTEXITCODE }
        } catch {
            $errorMessage = $_.Exception.Message
            Add-Content -Path $LogFile -Value "[$(Get-Date -Format o)] ERROR: $errorMessage" -Encoding UTF8
            throw
        } finally {
            if (Test-Path $stdoutTemp) {
                Add-Content -Path $LogFile -Value "[$(Get-Date -Format o)] STDOUT:" -Encoding UTF8
                Get-Content -Path $stdoutTemp -ErrorAction SilentlyContinue | ForEach-Object {
                    Add-Content -Path $LogFile -Value $_ -Encoding UTF8
                }
                Remove-Item -Path $stdoutTemp -Force -ErrorAction SilentlyContinue
                Add-Content -Path $LogFile -Value '' -Encoding UTF8
            }
            if (Test-Path $stderrTemp) {
                Add-Content -Path $LogFile -Value "[$(Get-Date -Format o)] STDERR:" -Encoding UTF8
                Get-Content -Path $stderrTemp -ErrorAction SilentlyContinue | ForEach-Object {
                    Add-Content -Path $LogFile -Value $_ -Encoding UTF8
                }
                Remove-Item -Path $stderrTemp -Force -ErrorAction SilentlyContinue
                Add-Content -Path $LogFile -Value '' -Encoding UTF8
            }
            Add-Content -Path $LogFile -Value "[$(Get-Date -Format o)] EXIT CODE: $exitCode" -Encoding UTF8
            Add-Content -Path $LogFile -Value '' -Encoding UTF8
        }
    } else {
        & $commandPath @commandArgs
        $exitCode = $LASTEXITCODE
    }

    $global:LASTEXITCODE = $exitCode
    return $exitCode
}

function Write-PipLogTail {
    param(
        [Parameter(Mandatory = $true)][string]$LogFile,
        [int]$Lines = 20
    )

    if (-not (Test-Path $LogFile)) {
        Write-Warn "Pip log $LogFile not found."
        return
    }

    Write-Warn "Last $Lines lines from pip log ($LogFile):"
    Get-Content -Path $LogFile -Tail $Lines -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "        $_" -ForegroundColor Yellow
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

$pipLog = Join-Path $logs "pip_${launcherStamp}.log"
try {
    "VideoCatalog pip session log started $(Get-Date -Format o)" | Set-Content -Path $pipLog -Encoding UTF8
    Write-Info "Pip output will be logged to $pipLog"
} catch {
    Write-Warn "Unable to initialize pip log at ${pipLog}: $($_.Exception.Message)"
    $pipLog = $null
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

    $systemPython = $null
    $pythonVersionText = $null

    if ($env:LOCALAPPDATA) {
        $preferredPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $preferredPython) {
            try {
                $systemPython = (Resolve-Path -LiteralPath $preferredPython).Path
                Write-Info "Found Python 3.12 at $systemPython"
            } catch {
                Write-Warn "Unable to resolve preferred Python path $preferredPython: $($_.Exception.Message)"
                $systemPython = $null
            }
        }
    }

    if (-not $systemPython) {
        $pyLauncherCmd = Get-Command py -ErrorAction SilentlyContinue
        if ($pyLauncherCmd) {
            $pyLauncherPath = $pyLauncherCmd.Source
            if (-not $pyLauncherPath) { $pyLauncherPath = $pyLauncherCmd.Path }
            if (-not $pyLauncherPath) { $pyLauncherPath = $pyLauncherCmd.Definition }
            if ($pyLauncherPath) {
                Write-Info "Windows Python launcher detected at $pyLauncherPath"
                try {
                    $launcherQuery = (& $pyLauncherPath '-3.12' '-c' 'import sys; print(sys.executable)').Trim()
                    if ($LASTEXITCODE -eq 0 -and $launcherQuery) {
                        $systemPython = $launcherQuery
                    }
                } catch {
                    Write-Warn "Unable to query the Windows launcher for Python 3.12: $($_.Exception.Message)"
                }
            }
        }
    }

    if (-not $systemPython) {
        Write-Err 'Python 3.12 is required. Install Python 3.12 from https://www.python.org/downloads/windows/ and ensure it is available at %LOCALAPPDATA%\Programs\Python\Python312\python.exe or via the "py -3.12" launcher.'
        exit 1
    }

    $pythonVersionText = (& $systemPython -c "import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")").Trim()
    & $systemPython -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" *> $null
    if ($LASTEXITCODE -ne 0) {
        if (-not $pythonVersionText) { $pythonVersionText = 'unknown' }
        Write-Err "Python $pythonVersionText detected, but Python 3.12 is required. Install Python 3.12 from https://www.python.org/downloads/windows/."
        exit 1
    }

    Write-Info "Using Python $pythonVersionText at $systemPython"

    $pipCmd = Get-Command pip -ErrorAction SilentlyContinue
    if ($pipCmd) {
        $systemPip = $pipCmd.Source
        if (-not $systemPip) { $systemPip = $pipCmd.Path }
        if (-not $systemPip) { $systemPip = $pipCmd.Definition }
        Write-Info "pip detected at $systemPip"
    } else {
        Write-Warn 'pip executable not found on PATH. Falling back to python -m pip.'
        $systemPip = $null
    }

    $venvPath = Join-Path $work 'venv'
    $venvScripts = Join-Path $venvPath 'Scripts'
    $venvPython = Join-Path $venvScripts 'python.exe'
    $venvPip = Join-Path $venvScripts 'pip.exe'

    if (-not (Test-Path $venvPython)) {
        Write-Info "Creating Python virtual environment at $venvPath"
        & $systemPython -m venv $venvPath
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
            throw "Failed to create Python virtual environment at $venvPath"
        }
    }

    $pythonExe = $venvPython
    if (Test-Path $venvPip) {
        $pipExe = $venvPip
        Write-Info "Using virtual environment pip at $pipExe"
    } else {
        Write-Warn 'Virtual environment pip executable missing. Falling back to python -m pip.'
        $pipExe = $null
    }
    Write-Info "Using virtual environment python at $pythonExe"

    $env:VIDEOCATALOG_HOME = $workFull
    $env:PYTHONUNBUFFERED = '1'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

    Write-Info 'Upgrading pip, setuptools, and wheel in the virtual environment.'
    $bootstrapArgs = @('install', '--upgrade', 'pip', 'setuptools', 'wheel')
    # Use ``python -m pip`` for the bootstrap upgrade to allow pip to replace its
    # own executable on Windows where running ``pip.exe`` can block self-upgrades.
    $bootstrapExit = Invoke-PipInstall -Arguments $bootstrapArgs -PythonExe $pythonExe -PipExe $null -LogFile $pipLog
    if ($bootstrapExit -ne 0) {
        $logHint = if ($pipLog) { " Review pip log at $pipLog." } else { '' }
        Write-Warn "Failed to upgrade pip/setuptools/wheel (exit code $bootstrapExit). Continuing with existing versions.$logHint"
    } else {
        if (-not $pipExe -and (Test-Path $venvPip)) {
            $pipExe = $venvPip
            Write-Info "Detected pip at $pipExe after upgrade."
        }
    }

    $uvicornAvailable = Test-PythonModule -PythonExe $pythonExe -ModuleName 'uvicorn'
    if (-not $uvicornAvailable) {
        $requirementsPath = Join-Path $root 'profiles\windows-cpu.txt'
        if (-not (Test-Path $requirementsPath)) {
            $requirementsPath = Join-Path $root 'requirements.txt'
        }
        Write-Warn 'uvicorn not found, installing required Python packages.'
        Write-Info "Installing dependencies from $requirementsPath"
        $baseInstallArgs = @('install', '--only-binary=:all:', '--require-hashes', '-r', $requirementsPath)
        $exitCode = Invoke-PipInstall -Arguments $baseInstallArgs -PythonExe $pythonExe -PipExe $pipExe -LogFile $pipLog
        if ($exitCode -ne 0) {
            if ($pipLog) {
                Write-PipLogTail -LogFile $pipLog -Lines 40
            }
            $message = 'Dependency installation failed.'
            if ($pipLog) {
                $message += " Review pip log at $pipLog for details."
            }
            throw $message
        }

        $pyVersionText = (& $pythonExe -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")').Trim()
        $pyVersionParts = $pyVersionText.Split('.')
        $pyTag = $null
        if ($pyVersionParts.Length -ge 2) {
            $pyTag = "cp{0}{1}" -f $pyVersionParts[0], $pyVersionParts[1]
        }

        $optionalPackages = @()

        if ($pyTag) {
            $llamaWheel = "https://github.com/abetlen/llama-cpp-python/releases/download/v0.2.90/llama_cpp_python-0.2.90-$pyTag-$pyTag-win_amd64.whl"
            $optionalPackages += [pscustomobject]@{
                Name = 'llama-cpp-python'
                Requirement = "llama-cpp-python @ $llamaWheel"
                ForceBinary = $false
                FailureMessage = "Local llama.cpp runtime will be unavailable. Install the Visual C++ Build Tools or install the prebuilt wheel manually from $llamaWheel."
            }
        }

        $optionalPackages += [pscustomobject]@{
            Name = 'hnswlib'
            Requirement = 'hnswlib==0.8.0'
            ForceBinary = $false
            FailureMessage = 'Falling back to FAISS/simple similarity search. Install the Visual C++ Build Tools to enable the hnswlib backend.'
        }

        $optionalPackages += [pscustomobject]@{
            Name = 'faiss-cpu'
            Requirement = 'faiss-cpu==1.7.4'
            ForceBinary = $true
            FailureMessage = 'Falling back to simple similarity search. Install FAISS manually to enable the FAISS backend.'
        }

        foreach ($package in $optionalPackages) {
            if (-not $package.Requirement) { continue }
            $args = @('install')
            if ($package.ForceBinary) {
                $args += '--only-binary=:all:'
            }
            $args += $package.Requirement
            Write-Info "Installing optional dependency $($package.Name)"
            $result = Invoke-PipInstall -Arguments $args -PythonExe $pythonExe -PipExe $pipExe -LogFile $pipLog
            if ($result -eq 0) {
                Write-Info "Optional dependency $($package.Name) installed."
                continue
            }

            $optionalFailure = $package.FailureMessage
            if ($pipLog) {
                $optionalFailure = "$optionalFailure See pip log at $pipLog."
            }
            Write-Warn "Unable to install optional dependency $($package.Name). $optionalFailure"
        }

        if (-not (Test-PythonModule -PythonExe $pythonExe -ModuleName 'uvicorn')) {
            if ($pipLog) {
                Write-PipLogTail -LogFile $pipLog -Lines 40
            }
            $uvicornMessage = 'uvicorn is still unavailable after installation.'
            if ($pipLog) {
                $uvicornMessage += " Review pip log at $pipLog for details."
            }
            throw $uvicornMessage
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
    $OrchStdOutLog = Join-Path $logs "orchestrator_${orchLogStamp}_out.log"
    $OrchStdErrLog = Join-Path $logs "orchestrator_${orchLogStamp}_err.log"
    $WebStdOutLog = Join-Path $logs "web_${orchLogStamp}_out.log"
    $WebStdErrLog = Join-Path $logs "web_${orchLogStamp}_err.log"

    $orchProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'orchestrator.scheduler', '--working-dir', $workFull) -WorkingDirectory $root -RedirectStandardOutput $OrchStdOutLog -RedirectStandardError $OrchStdErrLog -NoNewWindow -PassThru
    Write-Info "Orchestrator starting (PID $($orchProcess.Id)) logging stdout to $OrchStdOutLog and stderr to $OrchStdErrLog"

    $orchReady = $false
    $orchStart = Get-Date
    while (-not $orchReady -and -not $orchProcess.HasExited) {
        if ((Get-Date) - $orchStart -gt [TimeSpan]::FromSeconds(20)) {
            break
        }
        if (Test-Path $OrchStdOutLog) {
            $recent = Get-Content -Path $OrchStdOutLog -Tail 40 -ErrorAction SilentlyContinue
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

    $webProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'videocatalog_api', '--host', '127.0.0.1', '--port', '27182') -WorkingDirectory $root -RedirectStandardOutput $WebStdOutLog -RedirectStandardError $WebStdErrLog -NoNewWindow -PassThru
    Write-Info "Web server starting (PID $($webProcess.Id)) logging stdout to $WebStdOutLog and stderr to $WebStdErrLog"

    $webReady = $false
    $webStart = Get-Date
    while (-not $webReady -and -not $webProcess.HasExited) {
        if ((Get-Date) - $webStart -gt [TimeSpan]::FromSeconds(25)) {
            break
        }
        if (Test-Path $WebStdOutLog) {
            $recentWeb = Get-Content -Path $WebStdOutLog -Tail 40 -ErrorAction SilentlyContinue
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
    Write-Info "  Orchestrator stdout log: $OrchStdOutLog"
    Write-Info "  Orchestrator stderr log: $OrchStdErrLog"
    Write-Info "  Web stdout log: $WebStdOutLog"
    Write-Info "  Web stderr log: $WebStdErrLog"

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
    $errorText = if ($_.Exception) { $_.Exception.ToString() } else { $_.ToString() }
    Write-Err $errorText
    if ($pipLog) {
        Write-Info "Pip log for this session: $pipLog"
    }
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
