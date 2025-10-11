"""Preflight diagnostics for VideoCatalog."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import db as core_db
from core.paths import get_catalog_db_path, get_logs_dir, resolve_working_dir
from core.settings import load_settings
from core.settings_schema import SETTINGS_VALIDATOR
from gpu.capabilities import probe_gpu

from .logs import EVENT_RANGES, log_event, persist_snapshot, purge_old_logs

MIN_VRAM_GB = 8.0


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    severity: str
    message: str
    hint: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PreflightResult:
    ts: float
    gpu_ready: bool
    sections: Dict[str, Dict[str, Any]]
    checks: List[CheckResult]

    def summary(self) -> Dict[str, int]:
        summary = {"MAJOR": 0, "MINOR": 0, "INFO": 0}
        for check in self.checks:
            summary[check.severity.upper()] = summary.get(check.severity.upper(), 0) + (0 if check.ok else 1)
        return summary


def _version_of(cmd: List[str]) -> Optional[str]:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (FileNotFoundError, PermissionError, subprocess.SubprocessError):
        return None
    output = completed.stdout or completed.stderr or ""
    for line in output.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _check_gpu(*, settings: Dict[str, Any], working_dir: Path) -> Tuple[bool, Dict[str, Any], CheckResult]:
    start = time.perf_counter()
    try:
        caps = probe_gpu(refresh=True)
    except Exception as exc:  # pragma: no cover - defensive
        duration = (time.perf_counter() - start) * 1000
        log_event(
            event_id=EVENT_RANGES["preflight"][0],
            level="ERROR",
            module="diagnostics.preflight",
            op="gpu_probe",
            duration_ms=duration,
            ok=False,
            err_code="GPU_PROBE_FAIL",
            hint="Install NVIDIA drivers and retry",
            extra={"error": str(exc)},
            working_dir=working_dir,
        )
        data = {"present": False, "name": None, "vram_gb": 0.0, "cuda_ok": False, "driver_ver": None, "free_vram_gb": 0.0}
        check = CheckResult(
            name="gpu",
            ok=False,
            severity="MAJOR",
            message="GPU probe failed",
            hint="Install NVIDIA drivers and CUDA runtime",
            data=data,
        )
        return False, data, check

    total_bytes = caps.get("nv_vram_bytes") or 0
    free_bytes = caps.get("nv_free_vram_bytes") or 0
    vram_gb = float(total_bytes) / (1024 ** 3) if total_bytes else 0.0
    free_gb = float(free_bytes) / (1024 ** 3) if free_bytes else 0.0
    present = bool(caps.get("has_nvidia"))
    driver = caps.get("nv_driver_version")
    cuda_runtime = bool(caps.get("cuda_available"))
    cuda_ok = bool(caps.get("onnx_cuda_ok"))
    ready = present and vram_gb >= MIN_VRAM_GB and cuda_runtime and cuda_ok
    severity = "INFO" if ready else "MAJOR"
    hint = None
    message = "GPU ready"
    if not present:
        message = "GPU not ready: NVIDIA adapter missing"
        hint = "Install a supported NVIDIA GPU (>=8GB VRAM)."
    elif vram_gb < MIN_VRAM_GB:
        message = f"GPU not ready: only {vram_gb:.1f} GiB VRAM detected"
        hint = "Upgrade GPU or free VRAM; VideoCatalog requires >=8GB."
    elif not cuda_runtime:
        message = "GPU not ready: CUDA runtime unavailable"
        hint = "Install CUDA runtime/driver package."
    elif not cuda_ok:
        message = "GPU not ready: ONNX CUDA provider failed"
        hint = "Install onnxruntime-gpu dependencies (CUDA, cuDNN)."

    details = {
        "present": present,
        "name": caps.get("nv_name"),
        "vram_gb": round(vram_gb, 2),
        "free_vram_gb": round(free_gb, 2),
        "cuda_ok": cuda_ok,
        "cuda_runtime": cuda_runtime,
        "driver_ver": driver,
        "directml_ok": bool(caps.get("onnx_directml_ok")),
    }
    duration = (time.perf_counter() - start) * 1000
    log_event(
        event_id=EVENT_RANGES["preflight"][0],
        level="INFO" if ready else "ERROR",
        module="diagnostics.preflight",
        op="gpu",
        duration_ms=duration,
        ok=ready,
        hint=hint,
        extra=details,
        working_dir=working_dir,
    )
    check = CheckResult(name="gpu", ok=ready, severity=severity, message=message, hint=hint, data=details)
    return ready, details, check


def _check_tools(*, working_dir: Path) -> Tuple[Dict[str, Any], List[CheckResult]]:
    tools = {
        "ffprobe": {"cmd": ["ffprobe", "-version"]},
        "tesseract": {"cmd": ["tesseract", "--version"]},
    }
    statuses: Dict[str, Any] = {}
    checks: List[CheckResult] = []
    for name, meta in tools.items():
        start = time.perf_counter()
        version = _version_of(meta["cmd"])
        ok = version is not None
        severity = "INFO" if ok else "MINOR"
        hint = None if ok else f"Install {name} and ensure it is on PATH."
        statuses[name] = {"present": ok, "version": version}
        log_event(
            event_id=EVENT_RANGES["preflight"][0] + 1,
            level="INFO" if ok else "WARNING",
            module="diagnostics.preflight",
            op=f"tool_{name}",
            duration_ms=(time.perf_counter() - start) * 1000,
            ok=ok,
            hint=hint,
            working_dir=working_dir,
        )
        message = f"{name} detected" if ok else f"{name} missing"
        checks.append(CheckResult(name=f"tool_{name}", ok=ok, severity=severity, message=message, hint=hint, data=statuses[name]))
    return statuses, checks


def _check_api_keys(settings: Dict[str, Any], *, working_dir: Path) -> Tuple[Dict[str, Any], List[CheckResult]]:
    structure_cfg = settings.get("structure") if isinstance(settings.get("structure"), dict) else {}
    tmdb_section = structure_cfg.get("tmdb") if isinstance(structure_cfg.get("tmdb"), dict) else {}
    opensubs_section = structure_cfg.get("opensubtitles") if isinstance(structure_cfg.get("opensubtitles"), dict) else {}
    tmdb_key = tmdb_section.get("api_key")
    tmdb_present = bool((tmdb_key or os.environ.get("TMDB_API_KEY")) and str((tmdb_key or "").strip()))
    opensubs_key = opensubs_section.get("api_key") or os.environ.get("OPENSUBTITLES_API_KEY")
    opensubs_enabled = bool(opensubs_section.get("enabled", True))
    opensubs_cached = False
    cache_path = working_dir / "cache" / "opensubs_auth.json"
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            opensubs_cached = bool(payload)
        except Exception:
            opensubs_cached = False
    statuses = {
        "tmdb_key_present": tmdb_present,
        "opensubs_auth_ok": (not opensubs_enabled) or bool(opensubs_key) or opensubs_cached,
    }
    checks = [
        CheckResult(
            name="tmdb_api",
            ok=tmdb_present,
            severity="INFO" if tmdb_present else "MINOR",
            message="TMDB API key configured" if tmdb_present else "TMDB API key missing",
            hint=None if tmdb_present else "Set structure.tmdb.api_key or TMDB_API_KEY env.",
            data={"present": tmdb_present},
        ),
    ]
    if opensubs_enabled:
        ok = bool(opensubs_key) or opensubs_cached
        hint = None if ok else "Configure OpenSubtitles API key or disable integration."
        checks.append(
            CheckResult(
                name="opensubs_api",
                ok=ok,
                severity="INFO" if ok else "MINOR",
                message="OpenSubtitles auth cached" if ok else "OpenSubtitles auth missing",
                hint=hint,
                data={"cached": opensubs_cached, "key_present": bool(opensubs_key)},
            )
        )
    else:
        checks.append(
            CheckResult(
                name="opensubs_api",
                ok=True,
                severity="INFO",
                message="OpenSubtitles disabled",
                hint=None,
                data={"cached": False, "key_present": False},
            )
        )
    for idx, check in enumerate(checks, start=2):
        log_event(
            event_id=EVENT_RANGES["preflight"][0] + idx,
            level="INFO" if check.ok else "WARNING",
            module="diagnostics.preflight",
            op=check.name,
            ok=check.ok,
            hint=check.hint,
            extra=check.data,
            working_dir=working_dir,
        )
    return statuses, checks


def _check_fs(*, working_dir: Path) -> Tuple[Dict[str, Any], List[CheckResult]]:
    logs_dir = get_logs_dir(working_dir)
    writable = True
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        test_file = logs_dir / ".diag_write_test"
        with open(test_file, "w", encoding="utf-8") as handle:
            handle.write("ok")
        test_file.unlink(missing_ok=True)
    except Exception:
        writable = False
    long_path_supported = os.name != "nt" or len(str(get_catalog_db_path(working_dir))) < 220
    temp_dir = Path(tempfile.gettempdir())
    try:
        usage = shutil.disk_usage(temp_dir)
        temp_space_gb = usage.free / (1024 ** 3)
    except Exception:
        temp_space_gb = None
    statuses = {
        "long_path_support": long_path_supported,
        "working_dir_writable": writable,
        "temp_space_gb": round(temp_space_gb, 2) if temp_space_gb is not None else None,
    }
    checks = [
        CheckResult(
            name="fs_workdir",
            ok=writable,
            severity="INFO" if writable else "MAJOR",
            message="Working directory writable" if writable else "Working directory not writable",
            hint=None if writable else f"Check permissions for {working_dir}",
            data={"path": str(working_dir)},
        ),
        CheckResult(
            name="fs_long_path",
            ok=long_path_supported,
            severity="INFO" if long_path_supported else "MINOR",
            message="Long path safe" if long_path_supported else "Potential long path issues",
            hint=None if long_path_supported else "Enable Windows long path support.",
            data={"path": str(get_catalog_db_path(working_dir))},
        ),
    ]
    if temp_space_gb is not None:
        ok_temp = temp_space_gb >= 1.0
        checks.append(
            CheckResult(
                name="fs_temp_space",
                ok=ok_temp,
                severity="INFO" if ok_temp else "MINOR",
                message=f"Temp space {temp_space_gb:.1f} GiB" if ok_temp else "Low temp space",
                hint=None if ok_temp else "Free disk space in system temp folder.",
                data={"temp_dir": str(temp_dir), "temp_space_gb": round(temp_space_gb, 2)},
            )
        )
    for idx, check in enumerate(checks, start=10):
        log_event(
            event_id=EVENT_RANGES["preflight"][0] + idx,
            level="INFO" if check.ok else "WARNING",
            module="diagnostics.preflight",
            op=check.name,
            ok=check.ok,
            hint=check.hint,
            extra=check.data,
            working_dir=working_dir,
        )
    return statuses, checks


def _check_db(*, working_dir: Path) -> Tuple[Dict[str, Any], List[CheckResult]]:
    db_path = get_catalog_db_path(working_dir)
    if not db_path.exists():
        data = {"path": str(db_path), "exists": False}
        check = CheckResult(
            name="db_presence",
            ok=False,
            severity="MINOR",
            message="Catalog database missing",
            hint="Run a scan to create catalog.db",
            data=data,
        )
        log_event(
            event_id=EVENT_RANGES["preflight"][0] + 20,
            level="WARNING",
            module="diagnostics.preflight",
            op="db_presence",
            ok=False,
            hint=check.hint,
            extra=data,
            working_dir=working_dir,
        )
        return data, [check]

    try:
        conn = core_db.connect(db_path, read_only=True, timeout=2.0)
    except sqlite3.Error as exc:
        data = {"path": str(db_path), "error": str(exc)}
        check = CheckResult(
            name="db_open",
            ok=False,
            severity="MAJOR",
            message="Catalog DB inaccessible",
            hint="Verify permissions and integrity.",
            data=data,
        )
        log_event(
            event_id=EVENT_RANGES["preflight"][0] + 21,
            level="ERROR",
            module="diagnostics.preflight",
            op="db_open",
            ok=False,
            err_code="DB_OPEN_FAIL",
            hint=check.hint,
            extra=data,
            working_dir=working_dir,
        )
        return data, [check]

    checks: List[CheckResult] = []
    data: Dict[str, Any] = {"path": str(db_path), "exists": True}
    try:
        conn.row_factory = sqlite3.Row
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()
        user_version = conn.execute("PRAGMA user_version").fetchone()
        wal_enabled = str(journal_mode[0]).lower() == "wal" if journal_mode else False
        busy_ok = int(busy_timeout[0]) >= core_db.DEFAULT_BUSY_TIMEOUT_MS if busy_timeout else False
        schema_version = int(user_version[0]) if user_version else 0
        data.update(
            {
                "wal_enabled": wal_enabled,
                "busy_timeout_ms": int(busy_timeout[0]) if busy_timeout else None,
                "schema_version": schema_version,
            }
        )
        checks.append(
            CheckResult(
                name="db_wal",
                ok=wal_enabled,
                severity="MAJOR" if not wal_enabled else "INFO",
                message="WAL enabled" if wal_enabled else "WAL disabled",
                hint=None if wal_enabled else "Enable WAL mode for better concurrency.",
                data={"wal": wal_enabled},
            )
        )
        checks.append(
            CheckResult(
                name="db_busy_timeout",
                ok=busy_ok,
                severity="MINOR" if busy_ok else "MINOR",
                message="busy_timeout configured" if busy_ok else "busy_timeout below recommended",
                hint=None if busy_ok else "Increase busy_timeout to >=5000 ms.",
                data={"busy_timeout_ms": data["busy_timeout_ms"]},
            )
        )
    finally:
        conn.close()
    for idx, check in enumerate(checks, start=25):
        log_event(
            event_id=EVENT_RANGES["preflight"][0] + idx,
            level="INFO" if check.ok else "WARNING",
            module="diagnostics.preflight",
            op=check.name,
            ok=check.ok,
            hint=check.hint,
            extra=check.data,
            working_dir=working_dir,
        )
    return data, checks


def _check_settings(settings: Dict[str, Any], *, working_dir: Path) -> Tuple[Dict[str, Any], List[CheckResult]]:
    unknown_keys = list(SETTINGS_VALIDATOR.unknown_keys(settings)) if isinstance(settings, dict) else []
    diagnostics_section = settings.get("diagnostics") if isinstance(settings.get("diagnostics"), dict) else {}
    timeouts = diagnostics_section.get("smoke_timeouts_s") if isinstance(diagnostics_section.get("smoke_timeouts_s"), dict) else {}
    invalid_timeouts = [name for name, value in timeouts.items() if (not isinstance(value, (int, float)) or value <= 0)]
    checks = []
    checks.append(
        CheckResult(
            name="settings_unknown",
            ok=not unknown_keys,
            severity="INFO" if not unknown_keys else "MINOR",
            message="Settings validated" if not unknown_keys else "Unknown settings keys detected",
            hint=None if not unknown_keys else ", ".join(unknown_keys[:8]),
            data={"unknown": unknown_keys},
        )
    )
    checks.append(
        CheckResult(
            name="settings_timeouts",
            ok=not invalid_timeouts,
            severity="INFO" if not invalid_timeouts else "MINOR",
            message="Smoke test timeouts valid" if not invalid_timeouts else "Invalid smoke test timeouts",
            hint=None if not invalid_timeouts else f"Fix diagnostics.smoke_timeouts_s for {', '.join(invalid_timeouts)}",
            data={"invalid_timeouts": invalid_timeouts},
        )
    )
    for idx, check in enumerate(checks, start=40):
        log_event(
            event_id=EVENT_RANGES["preflight"][0] + idx,
            level="INFO" if check.ok else "WARNING",
            module="diagnostics.preflight",
            op=check.name,
            ok=check.ok,
            hint=check.hint,
            extra=check.data,
            working_dir=working_dir,
        )
    return {"unknown_keys": unknown_keys, "invalid_timeouts": invalid_timeouts}, checks


def run_preflight(*, working_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Run preflight diagnostics and return a structured payload."""

    resolved_working_dir = working_dir or resolve_working_dir()
    settings = load_settings(resolved_working_dir) or {}
    diag_settings = settings.get("diagnostics") if isinstance(settings.get("diagnostics"), dict) else {}
    keep_days = int(diag_settings.get("logs_keep_days", 14)) if diag_settings else 14

    purge_old_logs(keep_days=keep_days, working_dir=resolved_working_dir)

    sections: Dict[str, Dict[str, Any]] = {}
    checks: List[CheckResult] = []

    gpu_ready, gpu_section, gpu_check = _check_gpu(settings=settings, working_dir=resolved_working_dir)
    sections["gpu"] = gpu_section
    checks.append(gpu_check)

    tools_section, tool_checks = _check_tools(working_dir=resolved_working_dir)
    sections["tools"] = tools_section
    checks.extend(tool_checks)

    api_section, api_checks = _check_api_keys(settings, working_dir=resolved_working_dir)
    sections["apis"] = api_section
    checks.extend(api_checks)

    fs_section, fs_checks = _check_fs(working_dir=resolved_working_dir)
    sections["fs"] = fs_section
    checks.extend(fs_checks)

    db_section, db_checks = _check_db(working_dir=resolved_working_dir)
    sections["db"] = db_section
    checks.extend(db_checks)

    settings_section, settings_checks = _check_settings(settings, working_dir=resolved_working_dir)
    sections["settings"] = settings_section
    checks.extend(settings_checks)

    result = PreflightResult(ts=time.time(), gpu_ready=gpu_ready, sections=sections, checks=checks)
    payload = {
        "ts": result.ts,
        "gpu_ready": result.gpu_ready,
        "summary": result.summary(),
        "sections": sections,
        "checks": [
            {
                "name": check.name,
                "ok": check.ok,
                "severity": check.severity,
                "message": check.message,
                "hint": check.hint,
                "data": check.data,
            }
            for check in checks
        ],
    }
    persist_snapshot("preflight", payload, working_dir=resolved_working_dir)
    return payload


__all__ = ["run_preflight"]
