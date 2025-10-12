"""Diagnostics preflight checks for VideoCatalog."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core import db as core_db
from core.paths import get_catalog_db_path, get_exports_dir, get_logs_dir, resolve_working_dir
from core.settings import load_settings
from core.settings_schema import SETTINGS_VALIDATOR
from gpu.capabilities import probe_gpu

from .logs import EVENT_RANGES, log_event, new_correlation_id, persist_snapshot, purge_old_logs

MIN_VRAM_GB = 8.0

REQUIRED_MODELS = [
    "text-embeddings.onnx",
    "vision-encoder.onnx",
]


@dataclass(slots=True)
class PreflightItem:
    code: str
    severity: str
    message: str
    where: str
    hint: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PreflightSection:
    name: str
    ok: bool
    details: Dict[str, Any]
    items: List[PreflightItem] = field(default_factory=list)


def _settings_for_diagnostics(settings: Dict[str, Any]) -> Dict[str, Any]:
    diag = settings.get("diagnostics")
    if isinstance(diag, dict):
        return diag
    return {}


def _check_gpu(*, working_dir: Path, diag_settings: Dict[str, Any]) -> Tuple[PreflightSection, bool]:
    start = time.perf_counter()
    correlation_id = new_correlation_id()
    gpu_info: Dict[str, Any] = {
        "present": False,
        "name": None,
        "driver_ver": None,
        "vram_total_gb": 0.0,
        "vram_free_gb": 0.0,
        "cuda_ok": False,
        "cuda_runtime": False,
        "models_present": {},
    }
    items: List[PreflightItem] = []
    ok = False
    err_code: Optional[str] = None
    hint: Optional[str] = None
    try:
        caps = probe_gpu(refresh=True)
    except Exception as exc:  # pragma: no cover - defensive
        err_code = "GPU_PROBE_FAIL"
        hint = "Install NVIDIA drivers with CUDA support."
        items.append(
            PreflightItem(
                code="GPU_NOT_READY",
                severity="MAJOR",
                message="Unable to query NVIDIA GPU capabilities",
                where="gpu",
                hint=hint,
                data={"error": str(exc)},
            )
        )
        duration = (time.perf_counter() - start) * 1000
        log_event(
            event_id=EVENT_RANGES["preflight"][0],
            level="ERROR",
            module="diagnostics.preflight",
            op="gpu_probe",
            working_dir=working_dir,
            correlation_id=correlation_id,
            duration_ms=duration,
            ok=False,
            err_code=err_code,
            hint=hint,
            details={"exception": str(exc)},
        )
        return PreflightSection(name="gpu", ok=False, details=gpu_info, items=items), False

    present = bool(caps.get("has_nvidia"))
    total_bytes = float(caps.get("nv_vram_bytes") or 0)
    free_bytes = float(caps.get("nv_free_vram_bytes") or 0)
    driver_version = caps.get("nv_driver_version")
    cuda_available = bool(caps.get("cuda_available"))
    cuda_ok = bool(caps.get("onnx_cuda_ok"))

    gpu_info.update(
        {
            "present": present,
            "name": caps.get("nv_name"),
            "driver_ver": driver_version,
            "vram_total_gb": round(total_bytes / (1024 ** 3), 2) if total_bytes else 0.0,
            "vram_free_gb": round(free_bytes / (1024 ** 3), 2) if free_bytes else 0.0,
            "cuda_ok": cuda_ok,
            "cuda_runtime": cuda_available,
        }
    )

    models_dir = working_dir / "models"
    model_status: Dict[str, bool] = {}
    for model in REQUIRED_MODELS:
        path = models_dir / model
        model_status[model] = path.exists()
    gpu_info["models_present"] = model_status

    ready = present and gpu_info["vram_total_gb"] >= MIN_VRAM_GB and cuda_available and cuda_ok
    ready = ready and all(model_status.values())
    ok = ready
    if not present:
        err_code = "GPU_MISSING"
        hint = "Install a compatible NVIDIA GPU with >=8GB VRAM."
    elif gpu_info["vram_total_gb"] < MIN_VRAM_GB:
        err_code = "GPU_INSUFFICIENT_VRAM"
        hint = "Upgrade to a GPU with at least 8GB VRAM or free memory."
    elif not cuda_available:
        err_code = "CUDA_RUNTIME_MISSING"
        hint = "Install the NVIDIA driver/CUDA runtime."
    elif not cuda_ok:
        err_code = "CUDA_PROVIDER_FAIL"
        hint = "Install onnxruntime-gpu dependencies (CUDA + cuDNN)."
    elif not all(model_status.values()):
        missing = [name for name, present_flag in model_status.items() if not present_flag]
        err_code = "MODEL_MISSING"
        hint = f"Download required models: {', '.join(missing)}"

    severity = "INFO"
    message = "GPU ready"
    if not ok:
        severity = "MAJOR"
        message = "GPU not ready"
        items.append(
            PreflightItem(
                code="GPU_NOT_READY",
                severity=severity,
                message=message,
                where="gpu",
                hint=hint,
                data=gpu_info.copy(),
            )
        )

    duration = (time.perf_counter() - start) * 1000
    log_event(
        event_id=EVENT_RANGES["preflight"][0],
        level="INFO" if ok else "ERROR",
        module="diagnostics.preflight",
        op="gpu",
        working_dir=working_dir,
        correlation_id=correlation_id,
        duration_ms=duration,
        ok=ok,
        err_code=err_code,
        hint=hint,
        details=gpu_info,
    )
    return PreflightSection(name="gpu", ok=ok, details=gpu_info, items=items), ok


def _capture_version(command: Iterable[str], timeout: float = 5.0) -> Optional[str]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout or completed.stderr
    if not output:
        return None
    line = output.splitlines()[0].strip()
    return line or None


def _check_tools(*, working_dir: Path) -> PreflightSection:
    start = time.perf_counter()
    correlation_id = new_correlation_id()
    info: Dict[str, Any] = {}
    items: List[PreflightItem] = []
    ffprobe_version = _capture_version(["ffprobe", "-version"])
    tesseract_version = _capture_version(["tesseract", "--version"])
    info["ffprobe"] = {"present": bool(ffprobe_version), "version": ffprobe_version}
    info["tesseract"] = {"present": bool(tesseract_version), "version": tesseract_version}

    ok = bool(ffprobe_version)
    if not ffprobe_version:
        items.append(
            PreflightItem(
                code="FFPROBE_MISSING",
                severity="MAJOR",
                message="ffprobe not found",
                where="tools",
                hint="Install FFmpeg/ffprobe and ensure it is on PATH.",
            )
        )
    if not tesseract_version:
        items.append(
            PreflightItem(
                code="TESSERACT_MISSING",
                severity="MINOR",
                message="tesseract not found (optional)",
                where="tools",
                hint="Install Tesseract OCR if document OCR is required.",
            )
        )

    duration = (time.perf_counter() - start) * 1000
    log_event(
        event_id=EVENT_RANGES["preflight"][0] + 1,
        level="INFO" if ok else "ERROR",
        module="diagnostics.preflight",
        op="tools",
        working_dir=working_dir,
        correlation_id=correlation_id,
        duration_ms=duration,
        ok=ok,
        err_code=None if ok else "TOOLS_MISSING",
        hint=None,
        details=info,
    )
    return PreflightSection(name="tools", ok=ok, details=info, items=items)


def _check_apis(settings: Dict[str, Any], *, working_dir: Path) -> PreflightSection:
    start = time.perf_counter()
    correlation_id = new_correlation_id()
    structure_cfg = settings.get("structure") if isinstance(settings.get("structure"), dict) else {}
    tmdb_cfg = structure_cfg.get("tmdb") if isinstance(structure_cfg.get("tmdb"), dict) else {}
    opensubs_cfg = structure_cfg.get("opensubtitles") if isinstance(structure_cfg.get("opensubtitles"), dict) else {}

    tmdb_key = tmdb_cfg.get("api_key") or os.environ.get("TMDB_API_KEY")
    opensubs_key = opensubs_cfg.get("api_key") or os.environ.get("OPENSUBTITLES_API_KEY")
    opensubs_enabled = bool(opensubs_cfg.get("enabled", True))

    cache_dir = working_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    opensubs_cache = cache_dir / "opensubs_auth.json"
    cache_cached = False
    if opensubs_cache.exists():
        try:
            payload = json.loads(opensubs_cache.read_text("utf-8"))
            cache_cached = bool(payload)
        except Exception:
            cache_cached = False

    info = {
        "tmdb_key_present": bool(tmdb_key),
        "opensubs_auth_present": bool(opensubs_key) or cache_cached or not opensubs_enabled,
        "opensubs_enabled": opensubs_enabled,
    }
    items: List[PreflightItem] = []
    ok = info["tmdb_key_present"] and info["opensubs_auth_present"]
    if not info["tmdb_key_present"]:
        items.append(
            PreflightItem(
                code="TMDB_KEY_MISSING",
                severity="MINOR",
                message="TMDB API key missing",
                where="apis",
                hint="Add structure.tmdb.api_key in settings.json or set TMDB_API_KEY.",
            )
        )
    if opensubs_enabled and not info["opensubs_auth_present"]:
        items.append(
            PreflightItem(
                code="OPENSUBS_AUTH_MISSING",
                severity="MINOR",
                message="OpenSubtitles credentials missing",
                where="apis",
                hint="Provide OpenSubtitles API key or disable the integration.",
            )
        )

    duration = (time.perf_counter() - start) * 1000
    log_event(
        event_id=EVENT_RANGES["preflight"][0] + 2,
        level="INFO" if ok else "WARNING",
        module="diagnostics.preflight",
        op="apis",
        working_dir=working_dir,
        correlation_id=correlation_id,
        duration_ms=duration,
        ok=ok,
        err_code=None if ok else "API_CREDENTIALS",
        hint=None,
        details=info,
    )
    return PreflightSection(name="apis", ok=ok, details=info, items=items)


def _check_filesystem(*, working_dir: Path) -> PreflightSection:
    start = time.perf_counter()
    correlation_id = new_correlation_id()
    info: Dict[str, Any] = {}
    items: List[PreflightItem] = []

    logs_dir = get_logs_dir(working_dir)
    exports_dir = get_exports_dir(working_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    writable = True
    try:
        test_path = logs_dir / ".diag_write"
        with open(test_path, "w", encoding="utf-8") as handle:
            handle.write("ok")
        test_path.unlink(missing_ok=True)
    except Exception as exc:
        writable = False
        items.append(
            PreflightItem(
                code="WORKDIR_NOT_WRITABLE",
                severity="MAJOR",
                message="Working directory not writable",
                where="filesystem",
                hint=f"Check permissions for {working_dir}",
                data={"error": str(exc)},
            )
        )

    usage = shutil.disk_usage(working_dir)
    free_gb = round(usage.free / (1024 ** 3), 2)
    info.update(
        {
            "working_dir": str(working_dir),
            "writable": writable,
            "free_space_gb": free_gb,
        }
    )
    safety_cap = 5.0
    if free_gb < safety_cap:
        items.append(
            PreflightItem(
                code="LOW_DISK_SPACE",
                severity="MINOR",
                message=f"Working dir free space below {safety_cap} GiB",
                where="filesystem",
                hint="Free disk space to avoid database corruption.",
                data={"free_space_gb": free_gb},
            )
        )

    duration = (time.perf_counter() - start) * 1000
    log_event(
        event_id=EVENT_RANGES["preflight"][0] + 3,
        level="INFO" if writable else "ERROR",
        module="diagnostics.preflight",
        op="filesystem",
        working_dir=working_dir,
        correlation_id=correlation_id,
        duration_ms=duration,
        ok=writable,
        err_code=None if writable else "WORKDIR",
        hint=None,
        details=info,
    )
    return PreflightSection(name="filesystem", ok=writable, details=info, items=items)


def _check_database(*, working_dir: Path) -> PreflightSection:
    start = time.perf_counter()
    correlation_id = new_correlation_id()
    db_path = get_catalog_db_path(working_dir)
    info = {"path": str(db_path), "exists": db_path.exists()}
    items: List[PreflightItem] = []
    ok = True

    if not db_path.exists():
        items.append(
            PreflightItem(
                code="DB_MISSING",
                severity="MINOR",
                message="catalog.db not found",
                where="database",
                hint="Run an inventory scan to create the catalog database.",
            )
        )
        ok = False
    else:
        try:
            conn = core_db.connect(db_path, read_only=True, timeout=2.0)
        except sqlite3.Error as exc:
            items.append(
                PreflightItem(
                    code="DB_OPEN_FAILED",
                    severity="MAJOR",
                    message="Unable to open catalog database",
                    where="database",
                    hint="Investigate file locks or corruption.",
                    data={"error": str(exc)},
                )
            )
            ok = False
        else:
            try:
                pragma_values = {
                    "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                    "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
                    "wal_autocheckpoint": conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0],
                }
                info.update(pragma_values)
                quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
                info["quick_check"] = quick_check
                if quick_check != "ok":
                    items.append(
                        PreflightItem(
                            code="DB_QUICK_CHECK_FAIL",
                            severity="MAJOR",
                            message="SQLite quick_check reported issues",
                            where="database",
                            hint="Run sqlite3 integrity_check and restore from backup if needed.",
                            data={"quick_check": quick_check},
                        )
                    )
                    ok = False
            except sqlite3.Error as exc:
                items.append(
                    PreflightItem(
                        code="DB_PRAGMA_FAIL",
                        severity="MINOR",
                        message="Unable to read database pragmas",
                        where="database",
                        hint="Ensure the database is not corrupted.",
                        data={"error": str(exc)},
                    )
                )
                ok = False
            finally:
                conn.close()

    duration = (time.perf_counter() - start) * 1000
    log_event(
        event_id=EVENT_RANGES["preflight"][0] + 4,
        level="INFO" if ok else "ERROR",
        module="diagnostics.preflight",
        op="database",
        working_dir=working_dir,
        correlation_id=correlation_id,
        duration_ms=duration,
        ok=ok,
        err_code=None if ok else "DB",
        hint=None,
        details=info,
    )
    return PreflightSection(name="database", ok=ok, details=info, items=items)


def _check_settings(settings: Dict[str, Any], *, working_dir: Path) -> PreflightSection:
    start = time.perf_counter()
    correlation_id = new_correlation_id()
    info: Dict[str, Any] = {"validated": False, "unknown_keys": []}
    items: List[PreflightItem] = []
    try:
        SETTINGS_VALIDATOR.validate(settings)
        info["validated"] = True
    except Exception as exc:  # pragma: no cover - schema validation rarely fails
        items.append(
            PreflightItem(
                code="SETTINGS_INVALID",
                severity="MINOR",
                message="Settings validation failed",
                where="settings",
                hint="Review settings.json and remove unsupported values.",
                data={"error": str(exc)},
            )
        )

    # Unknown keys relative to schema root
    schema_keys = set(SETTINGS_VALIDATOR.schema.get("properties", {}).keys())
    unknown_keys = [key for key in settings.keys() if key not in schema_keys]
    if unknown_keys:
        info["unknown_keys"] = sorted(unknown_keys)
        items.append(
            PreflightItem(
                code="SETTINGS_UNKNOWN_KEYS",
                severity="MINOR",
                message="Settings contain unknown keys",
                where="settings",
                hint="Remove unused keys from settings.json to keep configuration clean.",
                data={"unknown_keys": info["unknown_keys"]},
            )
        )

    duration = (time.perf_counter() - start) * 1000
    log_event(
        event_id=EVENT_RANGES["preflight"][0] + 5,
        level="INFO" if not items else "WARNING",
        module="diagnostics.preflight",
        op="settings",
        working_dir=working_dir,
        correlation_id=correlation_id,
        duration_ms=duration,
        ok=not bool(items),
        err_code=None if not items else "SETTINGS",
        hint=None,
        details=info,
    )
    return PreflightSection(name="settings", ok=not bool(items), details=info, items=items)


def _ensure_diag_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diag_reports (
            id TEXT PRIMARY KEY,
            created_utc TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            items_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diag_metrics (
            ts_utc TEXT NOT NULL,
            series TEXT NOT NULL,
            labels_json TEXT NOT NULL,
            value REAL NOT NULL
        )
        """
    )


def _record_metric(conn: sqlite3.Connection, *, series: str, value: float, labels: Optional[Dict[str, Any]] = None) -> None:
    conn.execute(
        "INSERT INTO diag_metrics (ts_utc, series, labels_json, value) VALUES (?, ?, ?, ?)",
        (
            datetime.utcnow().isoformat(timespec="seconds"),
            series,
            json.dumps(labels or {}, ensure_ascii=False),
            float(value),
        ),
    )


def _store_report(conn: sqlite3.Connection, *, report_id: str, summary: Dict[str, int], items: List[PreflightItem]) -> None:
    payload = [
        {
            "code": item.code,
            "severity": item.severity,
            "message": item.message,
            "where": item.where,
            "hint": item.hint,
            "data": item.data,
        }
        for item in items
    ]
    conn.execute(
        "INSERT OR REPLACE INTO diag_reports (id, created_utc, summary_json, items_json) VALUES (?, ?, ?, ?)",
        (
            report_id,
            datetime.utcnow().isoformat(timespec="seconds"),
            json.dumps(summary, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def run_preflight(*, working_dir: Optional[Path] = None, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute the diagnostics preflight checks."""

    resolved = working_dir or resolve_working_dir()
    settings_payload = settings or load_settings(resolved) or {}
    diag_settings = _settings_for_diagnostics(settings_payload)
    keep_days = int(diag_settings.get("logs_keep_days", 14) or 14)
    purge_old_logs(keep_days=keep_days, working_dir=resolved)

    sections: List[PreflightSection] = []
    all_items: List[PreflightItem] = []

    gpu_section, gpu_ready = _check_gpu(working_dir=resolved, diag_settings=diag_settings)
    sections.append(gpu_section)
    all_items.extend(gpu_section.items)

    sections.append(tools := _check_tools(working_dir=resolved))
    all_items.extend(tools.items)

    sections.append(apis := _check_apis(settings_payload, working_dir=resolved))
    all_items.extend(apis.items)

    sections.append(fs := _check_filesystem(working_dir=resolved))
    all_items.extend(fs.items)

    sections.append(db_section := _check_database(working_dir=resolved))
    all_items.extend(db_section.items)

    sections.append(settings_section := _check_settings(settings_payload, working_dir=resolved))
    all_items.extend(settings_section.items)

    summary = {"major": 0, "minor": 0, "info": 0}
    for item in all_items:
        severity = item.severity.lower()
        if severity == "major":
            summary["major"] += 1
        elif severity == "minor":
            summary["minor"] += 1
        else:
            summary["info"] += 1

    assistant_enabled = bool(gpu_ready or not diag_settings.get("gpu_hard_requirement", True))
    if not assistant_enabled and all(item.code != "GPU_NOT_READY" for item in all_items):
        all_items.append(
            PreflightItem(
                code="GPU_NOT_READY",
                severity="MAJOR",
                message="AI assistant disabled due to GPU requirement",
                where="assistant",
                hint="Install a compatible NVIDIA GPU to enable AI features.",
                data={},
            )
        )
        summary["major"] += 1

    payload = {
        "id": datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "ts": time.time(),
        "assistant": {"enabled": assistant_enabled},
        "gpu_ready": gpu_ready,
        "summary": summary,
        "sections": {
            section.name: {
                "ok": section.ok,
                "details": section.details,
                "items": [
                    {
                        "code": item.code,
                        "severity": item.severity,
                        "message": item.message,
                        "where": item.where,
                        "hint": item.hint,
                        "data": item.data,
                    }
                    for item in section.items
                ],
            }
            for section in sections
        },
        "items": [
            {
                "code": item.code,
                "severity": item.severity,
                "message": item.message,
                "where": item.where,
                "hint": item.hint,
                "data": item.data,
            }
            for item in all_items
        ],
    }

    persist_snapshot("preflight", payload, working_dir=resolved)

    db_path = get_catalog_db_path(resolved)
    if db_path.exists():
        conn = core_db.connect(db_path, read_only=False, timeout=5.0)
        try:
            _ensure_diag_tables(conn)
            _store_report(conn, report_id=payload["id"], summary=summary, items=all_items)
            _record_metric(conn, series="preflight_ms", value=0.0, labels={"source": "preflight"})
            conn.commit()
        finally:
            conn.close()

    return payload


__all__ = ["run_preflight", "PreflightItem", "PreflightSection"]
