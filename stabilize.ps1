[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

$pythonCmd = (Get-Command python -ErrorAction Stop).Source

function Write-Log {
    param(
        [string]$Message,
        [string]$Level = 'INFO'
    )
    $timestamp = (Get-Date).ToString('s')
    $entry = "[$timestamp] [$Level] $Message"
    Write-Host $entry
    if ($script:SetupLog) {
        Add-Content -Path $script:SetupLog -Value $entry
    }
}

function Write-Step {
    param([string]$Message)
    Write-Host "== $Message" -ForegroundColor Cyan
    if ($script:SetupLog) {
        Add-Content -Path $script:SetupLog -Value ("[{0}] [STEP] {1}" -f (Get-Date).ToString('s'), $Message)
    }
}

$summary = @()
function Add-Summary {
    param(
        [string]$Step,
        [string]$Status,
        [string]$Detail
    )
    $script:summary += [pscustomobject]@{ Step = $Step; Status = $Status; Detail = $Detail }
}

Write-Step "0) Resolve working directory"
$workingDir = (& $pythonCmd -c "from core.paths import resolve_working_dir; print(resolve_working_dir())").Trim()
if (-not $workingDir) {
    throw "Unable to resolve working directory"
}
if (-not (Test-Path -LiteralPath $workingDir)) {
    New-Item -ItemType Directory -Path $workingDir | Out-Null
}
$logsDir = Join-Path $workingDir 'logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$script:SetupLog = Join-Path $logsDir 'stabilize.log'
"[{0}] stabilize.ps1 started" -f (Get-Date).ToString('s') | Set-Content -Path $script:SetupLog
Write-Log "Working directory -> $workingDir"
$env:VIDEOCATALOG_API_KEY = 'localdev'

# ---------------------------------------------------------------------------
# Step 1: Settings and paths
# ---------------------------------------------------------------------------
Write-Step "1) Settings & Paths"
$prepareLog = Join-Path $logsDir 'upgrade_prepare.log'
$prepareArgs = @('upgrade_db.py', '--prepare-only', '--log', $prepareLog)
$prepareOutput = & $pythonCmd @prepareArgs 2>&1
$prepareStatus = $LASTEXITCODE
$prepareOutput | ForEach-Object { Write-Log $_ }
if ($prepareStatus -ne 0) {
    Add-Summary 'Settings & Paths' 'FAIL' 'upgrade_db prepare phase failed'
    throw "upgrade_db prepare phase failed"
}
$prepareJson = $null
$prepareOutput | ForEach-Object {
    try {
        $prepareJson = $_ | ConvertFrom-Json -ErrorAction Stop
    } catch {}
}
if ($prepareJson) {
    Write-Log ("Settings updated: {0}" -f $prepareJson.updated_settings)
}
Add-Summary 'Settings & Paths' 'PASS' 'Directories prepared and settings validated'

# ---------------------------------------------------------------------------
# Step 2: Dependencies & probes
# ---------------------------------------------------------------------------
Write-Step "2) Dependencies & Probes"
if (-not $SkipInstall) {
    $requirementsPath = Join-Path $repoRoot 'profiles\windows-cpu.txt'
    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        throw "profiles\\windows-cpu.txt not found"
    }
    Write-Log "Installing pinned dependencies from $requirementsPath"
    $installArgs = @('-m', 'pip', 'install', '--only-binary=:all:', '--requirement', $requirementsPath)
    & $pythonCmd @installArgs 2>&1 | ForEach-Object { Write-Log $_ }
    if ($LASTEXITCODE -ne 0) {
        Add-Summary 'Dependencies' 'FAIL' 'Dependency installation failed'
        throw "Dependency installation failed"
    }
    Add-Summary 'Dependencies' 'PASS' 'Python dependencies installed'
} else {
    Write-Log "SkipInstall flag set; skipping dependency installation"
    Add-Summary 'Dependencies' 'SKIP' 'Dependency installation skipped by flag'
}

# Probe ffprobe
$ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
if ($null -eq $ffprobe) {
    Write-Host "*** FFPROBE NOT DETECTED - QUALITY HEADERS DISABLED ***" -ForegroundColor Yellow
    $flagPath = Join-Path $logsDir 'quality_headers.disabled'
    "Install FFmpeg/ffprobe and re-run stabilize.ps1 to enable header checks." | Set-Content -Path $flagPath
    Write-Log "ffprobe missing; created $flagPath" 'WARN'
} else {
    $ffprobeVersion = (& $ffprobe.Source '-version' 2>$null | Select-Object -First 1)
    Write-Log ("ffprobe detected -> {0}" -f ($ffprobeVersion -or 'unknown'))
}

# Probe GPU
$gpuProbe = & $pythonCmd -c "import json\nfrom gpu.capabilities import probe_gpu\nprint(json.dumps(probe_gpu()))"
$gpuInfo = $null
try {
    $gpuInfo = $gpuProbe | ConvertFrom-Json -ErrorAction Stop
} catch {
    Write-Log "GPU probe failed: $_" 'WARN'
}
$gpuReady = $false
if ($gpuInfo) {
    $gpuReady = [bool]$gpuInfo.has_nvidia
    if (-not $gpuReady) {
        $reason = $gpuInfo.nv_error
        if (-not $reason) { $reason = 'No NVIDIA GPU detected' }
        Write-Host "*** GPU FEATURES DISABLED: $reason ***" -ForegroundColor White -BackgroundColor DarkRed
        $banner = Join-Path $logsDir 'gpu.disabled.banner'
        "GPU prerequisites not satisfied: $reason" | Set-Content -Path $banner
        Write-Log "GPU disabled: $reason" 'WARN'
    } else {
        Write-Log ("GPU detected -> {0}" -f ($gpuInfo.nv_name -or 'NVIDIA'))
    }
}

# ---------------------------------------------------------------------------
# Step 3: Model cache
# ---------------------------------------------------------------------------
Write-Step "3) Model cache"
$modelLog = Join-Path $logsDir 'model_cache.log'
$modelArgs = @('-m', 'assistant.model_cache', '--log', $modelLog)
$modelOutput = & $pythonCmd @modelArgs 2>&1
$modelStatus = $LASTEXITCODE
$modelOutput | ForEach-Object { Write-Log $_ }
if ($modelStatus -ne 0) {
    Add-Summary 'Model cache' 'FAIL' 'assistant.model_cache bootstrap failed'
    throw "assistant.model_cache bootstrap failed"
}
$modelJson = $null
$modelOutput | ForEach-Object {
    try {
        $modelJson = $_ | ConvertFrom-Json -ErrorAction Stop
    } catch {}
}
$modelDetail = 'Model cache prepared'
if ($modelJson -and $modelJson.models) {
    $aliases = @()
    $refreshed = @()
    foreach ($entry in $modelJson.models) {
        if ($entry.alias) { $aliases += $entry.alias }
        if ($entry.downloaded) { $refreshed += $entry.alias }
    }
    $count = ($modelJson.models | Measure-Object).Count
    if ($count -gt 0) {
        $modelDetail = "{0} models ready ({1})" -f $count, ($aliases -join ', ')
        if ($refreshed.Count -gt 0) {
            $modelDetail += "; refreshed: " + ($refreshed -join ', ')
        }
    }
}
Add-Summary 'Model cache' 'PASS' $modelDetail

# ---------------------------------------------------------------------------
# Step 4: Database migrations
# ---------------------------------------------------------------------------
Write-Step "4) Database migrations"
$upgradeLog = Join-Path $logsDir 'upgrade_run.log'
$upgradeArgs = @('upgrade_db.py', '--log', $upgradeLog)
$upgradeOutput = & $pythonCmd @upgradeArgs 2>&1
$upgradeStatus = $LASTEXITCODE
$upgradeOutput | ForEach-Object { Write-Log $_ }
if ($upgradeStatus -ne 0) {
    Add-Summary 'Database migrations' 'FAIL' 'upgrade_db execution failed'
    throw "upgrade_db execution failed"
}
Add-Summary 'Database migrations' 'PASS' 'Schema ensured for catalog and orchestrator'

# ---------------------------------------------------------------------------
# Step 5: Launch API server
# ---------------------------------------------------------------------------
Write-Step "5) Launch API server"
$serverLog = Join-Path $logsDir 'api-server.log'
if (Test-Path -LiteralPath $serverLog) { Remove-Item -LiteralPath $serverLog -Force }
$serverProcess = $null
try {
    $serverArgs = @('videocatalog_api.py')
    $serverProcess = Start-Process -FilePath $pythonCmd -ArgumentList $serverArgs -WorkingDirectory $repoRoot -RedirectStandardOutput $serverLog -RedirectStandardError $serverLog -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 200
    Write-Log "Waiting for API to become ready"
    $healthOk = $false
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        try {
            $headers = @{ 'X-API-Key' = $env:VIDEOCATALOG_API_KEY }
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:27182/v1/health' -Headers $headers -TimeoutSec 5 -ErrorAction Stop
            if ($response.StatusCode -eq 200) {
                $healthOk = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $healthOk) {
        Add-Summary 'API server' 'FAIL' 'Server did not become ready'
        throw "API server failed to start"
    }
    Add-Summary 'API server' 'PASS' 'API reachable on http://127.0.0.1:27182'

    # Optional orchestrator warmup when GPU present
    if ($gpuReady) {
        Write-Log "Queueing orchestrator warmup jobs"
        $orchScript = @'
import json
import sys
import time
from core.paths import resolve_working_dir
from core.settings import load_settings
from orchestrator.api import OrchestratorConfig, OrchestratorService

working = resolve_working_dir()
settings = load_settings(working)
service = OrchestratorService(OrchestratorConfig(working_dir=str(working), settings=settings))
if not service.enabled:
    print(json.dumps({"status": "skipped", "reason": "orchestrator_disabled"}))
    sys.exit(0)

service.start()
scheduler = service.scheduler
jobs = []
pending = []
try:
    for kind in ("assistant_warmup", "quality_headers"):
        try:
            job_id = scheduler.enqueue(kind, {"source": "stabilize"})
        except Exception as exc:
            print(json.dumps({"status": "enqueue_failed", "kind": kind, "error": str(exc)}))
            sys.exit(1)
        jobs.append((kind, job_id))
    pending = list(jobs)
    states = {}
    deadline = time.time() + 120
    while time.time() < deadline and pending:
        next_pending = []
        for kind, job_id in pending:
            detail = scheduler.get_job(job_id)
            if not detail:
                next_pending.append((kind, job_id))
                continue
            state = detail.get("status")
            states[kind] = state
            if state not in {"done", "failed"}:
                next_pending.append((kind, job_id))
        if not next_pending:
            pending = []
            break
        pending = next_pending
        time.sleep(2)

    details = []
    for kind, job_id in jobs:
        detail = scheduler.get_job(job_id)
        if detail:
            states[kind] = detail.get("status")
            details.append(
                {
                    "kind": kind,
                    "status": detail.get("status"),
                    "job_id": detail.get("id"),
                    "started": detail.get("started_utc"),
                    "ended": detail.get("ended_utc"),
                    "error": detail.get("error_msg"),
                }
            )
        else:
            states.setdefault(kind, "missing")

    result = {"status": "ok", "states": states, "jobs": details}
    if pending:
        result["status"] = "timeout"
        print(json.dumps(result))
        sys.exit(1)
    failures = {k: v for k, v in states.items() if v != "done"}
    if failures:
        result["status"] = "failed"
        print(json.dumps(result))
        sys.exit(1)
    print(json.dumps(result))
    sys.exit(0)
finally:
    service.stop()
'@
        $orchResult = & $pythonCmd - <<$orchScript 2>&1
        $orchExit = $LASTEXITCODE
        $orchResult | ForEach-Object { Write-Log $_ }
        $orchJson = $null
        $orchResult | ForEach-Object {
            try {
                $orchJson = $_ | ConvertFrom-Json -ErrorAction Stop
            } catch {}
        }
        if ($orchExit -eq 0 -and $orchJson -and $orchJson.status -eq 'ok') {
            $states = @()
            if ($orchJson.states) {
                foreach ($property in $orchJson.states.PSObject.Properties) {
                    $states += ($property.Name + '=' + $property.Value)
                }
            }
            if (-not $states) { $states = @('assistant_warmup=done', 'quality_headers=done') }
            Add-Summary 'Orchestrator warmup' 'PASS' ('Jobs completed: ' + ($states -join ', '))
        } elseif ($orchJson -and $orchJson.status -eq 'skipped') {
            Add-Summary 'Orchestrator warmup' 'SKIP' 'Orchestrator disabled in settings'
        } else {
            $detail = 'Warmup failed'
            if ($orchJson) {
                if ($orchJson.status -eq 'timeout') {
                    $detail = 'Warmup timed out'
                } elseif ($orchJson.status -eq 'enqueue_failed' -and $orchJson.kind) {
                    $detail = 'Warmup enqueue failed for ' + $orchJson.kind
                } elseif ($orchJson.states) {
                    $stateStrings = @()
                    foreach ($property in $orchJson.states.PSObject.Properties) {
                        $stateStrings += ($property.Name + '=' + $property.Value)
                    }
                    if ($stateStrings.Count -gt 0) {
                        $detail = 'Warmup failed (' + ($stateStrings -join ', ') + ')'
                    }
                } elseif ($orchJson.reason) {
                    $detail = 'Warmup skipped: ' + $orchJson.reason
                }
            }
            Add-Summary 'Orchestrator warmup' 'FAIL' $detail
        }
    } else {
        Write-Log "Skipping orchestrator warmup (GPU not ready)"
        Add-Summary 'Orchestrator warmup' 'SKIP' 'GPU not ready'
    }

    # -----------------------------------------------------------------------
    # Step 6: Diagnostics
    # -----------------------------------------------------------------------
    Write-Step "6) Diagnostics"
    $preflightArgs = @('web_preflight.py', '--timeout', '15')
    $preflightOutput = & $pythonCmd @preflightArgs 2>&1
    $preflightOutput | ForEach-Object { Write-Log $_ }
    $preflightStatus = $LASTEXITCODE
    if ($preflightStatus -ne 0) {
        Add-Summary 'Preflight' 'FAIL' 'web_preflight.py reported failures'
    } else {
        Add-Summary 'Preflight' 'PASS' 'web_preflight.py completed'
    }

    $smokeArgs = @('web_smoke.py', '--timeout', '20')
    $smokeOutput = & $pythonCmd @smokeArgs 2>&1
    $smokeOutput | ForEach-Object { Write-Log $_ }
    $smokeStatus = $LASTEXITCODE
    if ($smokeStatus -ne 0) {
        Add-Summary 'Smoke' 'FAIL' 'web_smoke.py reported failures'
    } else {
        Add-Summary 'Smoke' 'PASS' 'web_smoke.py completed'
    }

    # -----------------------------------------------------------------------
    # Step 7: Assistant ping
    # -----------------------------------------------------------------------
    Write-Step "7) Assistant sanity check"
    try {
        $headers = @{ 'X-API-Key' = $env:VIDEOCATALOG_API_KEY }
        $assistantStatus = Invoke-WebRequest -Uri 'http://127.0.0.1:27182/v1/assistant/status' -Headers $headers -TimeoutSec 10
        if ($assistantStatus.StatusCode -eq 200) {
            Add-Summary 'Assistant status' 'PASS' 'assistant/status reachable'
        } else {
            Add-Summary 'Assistant status' 'FAIL' ('assistant/status -> {0}' -f $assistantStatus.StatusCode)
        }
    } catch {
        Add-Summary 'Assistant status' 'FAIL' 'assistant/status request failed'
        Write-Log "assistant/status request failed: $_" 'WARN'
    }

} finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        try {
            Stop-Process -Id $serverProcess.Id -Force
        } catch {}
    }
}

Write-Host ""
Write-Host "== Summary ==" -ForegroundColor Cyan
$exitCode = 0
foreach ($entry in $summary) {
    Write-Host ("[{0}] {1} - {2}" -f $entry.Status, $entry.Step, $entry.Detail)
    if ($entry.Status -eq 'FAIL') {
        $exitCode = 1
    }
}
Write-Host "Logs: $script:SetupLog"
exit $exitCode
