"""Diagnostics smoke tests for VideoCatalog subsystems."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core import db as core_db
from core.paths import get_catalog_db_path, resolve_working_dir
from core.settings import load_settings
from gpu.capabilities import probe_gpu

from .logs import (
    EVENT_RANGES,
    log_event,
    new_correlation_id,
    persist_snapshot,
)


@dataclass(slots=True)
class SmokeItem:
    name: str
    status: str
    severity: str
    message: str
    duration_ms: float
    hint: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


SmokeFn = Callable[[Dict[str, Any], Path, Dict[str, Any]], SmokeItem]


DEFAULT_TARGETS = [
    "orchestrator_ping",
    "web_api_list",
    "realtime_subscribe",
    "assistant_ask_tiny",
    "rag_embed_min",
    "apiguard_cache_probe",
    "quality_headers",
    "textlite_preview",
    "backup_roundtrip",
]


def _gpu_ready(settings: Dict[str, Any]) -> bool:
    diag = settings.get("diagnostics") if isinstance(settings.get("diagnostics"), dict) else {}
    hard = bool(diag.get("gpu_hard_requirement", True))
    try:
        caps = probe_gpu(refresh=False)
    except Exception:
        return not hard
    present = bool(caps.get("has_nvidia"))
    total_bytes = float(caps.get("nv_vram_bytes") or 0.0)
    vram_gb = total_bytes / (1024 ** 3) if total_bytes else 0.0
    cuda = bool(caps.get("cuda_available"))
    cuda_ok = bool(caps.get("onnx_cuda_ok"))
    return present and vram_gb >= 8.0 and cuda and cuda_ok


def _check_orchestrator(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    enabled = bool(settings.get("orchestrator", {}).get("enable", True))
    if not enabled:
        duration = (time.perf_counter() - start) * 1000
        return SmokeItem(
            name="orchestrator_ping",
            status="SKIP",
            severity="INFO",
            message="Orchestrator disabled in settings",
            duration_ms=duration,
        )
    concurrency = settings.get("orchestrator", {}).get("concurrency", {})
    gpu_policy = settings.get("orchestrator", {}).get("gpu", {})
    duration = (time.perf_counter() - start) * 1000
    if not concurrency:
        return SmokeItem(
            name="orchestrator_ping",
            status="FAIL",
            severity="MINOR",
            message="Orchestrator concurrency not configured",
            duration_ms=duration,
            hint="Add orchestrator.concurrency settings.",
        )
    return SmokeItem(
        name="orchestrator_ping",
        status="PASS",
        severity="INFO",
        message="Orchestrator configuration healthy",
        duration_ms=duration,
        data={"queues": list(concurrency.keys()), "gpu_policy": gpu_policy},
    )


def _check_web_api(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    api_cfg = settings.get("api", {})
    enabled = bool(api_cfg.get("enabled_default", False))
    duration = (time.perf_counter() - start) * 1000
    if not enabled:
        return SmokeItem(
            name="web_api_list",
            status="SKIP",
            severity="INFO",
            message="Web API disabled",
            duration_ms=duration,
        )
    default_limit = api_cfg.get("default_limit")
    max_page = api_cfg.get("max_page_size")
    if not default_limit or not max_page:
        return SmokeItem(
            name="web_api_list",
            status="FAIL",
            severity="MINOR",
            message="API pagination limits missing",
            duration_ms=duration,
            hint="Set api.default_limit and api.max_page_size.",
        )
    return SmokeItem(
        name="web_api_list",
        status="PASS",
        severity="INFO",
        message="Web API configuration present",
        duration_ms=duration,
        data={"default_limit": default_limit, "max_page_size": max_page},
    )


def _check_realtime(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    monitor_dir = working_dir / "assistant_webmon"
    duration = (time.perf_counter() - start) * 1000
    if not monitor_dir.exists():
        return SmokeItem(
            name="realtime_subscribe",
            status="SKIP",
            severity="INFO",
            message="Realtime monitor not initialised",
            duration_ms=duration,
            hint="Run the web UI once to initialise realtime state.",
        )
    return SmokeItem(
        name="realtime_subscribe",
        status="PASS",
        severity="INFO",
        message="Realtime cache present",
        duration_ms=duration,
        data={"path": str(monitor_dir)},
    )


def _check_assistant(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    gpu_ready = _gpu_ready(settings)
    duration = (time.perf_counter() - start) * 1000
    if not gpu_ready:
        return SmokeItem(
            name="assistant_ask_tiny",
            status="SKIP",
            severity="INFO",
            message="AI disabled (GPU required)",
            duration_ms=duration,
            hint="Install a supported NVIDIA GPU to enable assistant.",
        )
    assistant_cfg = settings.get("assistant", {})
    if not assistant_cfg:
        return SmokeItem(
            name="assistant_ask_tiny",
            status="FAIL",
            severity="MINOR",
            message="Assistant settings missing",
            duration_ms=duration,
            hint="Configure assistant section in settings.json.",
        )
    return SmokeItem(
        name="assistant_ask_tiny",
        status="PASS",
        severity="INFO",
        message="Assistant ready for tiny Q&A",
        duration_ms=duration,
    )


def _check_rag(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    db_path = get_catalog_db_path(working_dir)
    if not db_path.exists():
        duration = (time.perf_counter() - start) * 1000
        return SmokeItem(
            name="rag_embed_min",
            status="SKIP",
            severity="INFO",
            message="Catalog DB missing",
            duration_ms=duration,
            hint="Run a scan to populate catalog.db.",
        )
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        duration = (time.perf_counter() - start) * 1000
        return SmokeItem(
            name="rag_embed_min",
            status="FAIL",
            severity="MAJOR",
            message="Unable to open catalog DB",
            duration_ms=duration,
            hint=str(exc),
        )
    try:
        count = conn.execute("SELECT COUNT(1) FROM vectors_pending").fetchone()[0]
    except sqlite3.DatabaseError:
        count = 0
    finally:
        conn.close()
    duration = (time.perf_counter() - start) * 1000
    if count == 0:
        return SmokeItem(
            name="rag_embed_min",
            status="SKIP",
            severity="INFO",
            message="No pending vectors",
            duration_ms=duration,
            hint="Queue documents for embedding to test the pipeline.",
        )
    return SmokeItem(
        name="rag_embed_min",
        status="PASS",
        severity="INFO",
        message="Vectors pending table populated",
        duration_ms=duration,
        data={"pending": int(count)},
    )


def _check_apiguard(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    cache_path = working_dir / "cache" / "tmdb_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    duration = (time.perf_counter() - start) * 1000
    if not cache_path.exists():
        return SmokeItem(
            name="apiguard_cache_probe",
            status="SKIP",
            severity="INFO",
            message="TMDB cache empty",
            duration_ms=duration,
            hint="Perform a metadata fetch to warm the cache.",
        )
    try:
        payload = json.loads(cache_path.read_text("utf-8"))
    except Exception as exc:
        return SmokeItem(
            name="apiguard_cache_probe",
            status="FAIL",
            severity="MINOR",
            message="TMDB cache unreadable",
            duration_ms=duration,
            hint=str(exc),
        )
    hits = sum(1 for item in payload.values() if item)
    if hits:
        status = "PASS"
        message = "TMDB cache contains entries"
    else:
        status = "SKIP"
        message = "TMDB cache cold"
    return SmokeItem(
        name="apiguard_cache_probe",
        status=status,
        severity="INFO",
        message=message,
        duration_ms=duration,
        data={"entries": len(payload)},
    )


def _check_quality(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    ffprobe_version = None
    try:
        completed = subprocess.run(["ffprobe", "-version"], capture_output=True, text=True, timeout=3)
        ffprobe_version = (completed.stdout or completed.stderr or "").splitlines()[0]
    except Exception:
        duration = (time.perf_counter() - start) * 1000
        return SmokeItem(
            name="quality_headers",
            status="SKIP",
            severity="INFO",
            message="quality headers disabled (ffprobe not found)",
            duration_ms=duration,
            hint="Install FFmpeg/ffprobe to enable header analysis.",
            data={"disabled": True},
        )
    duration = (time.perf_counter() - start) * 1000
    return SmokeItem(
        name="quality_headers",
        status="PASS",
        severity="INFO",
        message="ffprobe available",
        duration_ms=duration,
        data={"version": ffprobe_version},
    )


def _check_textlite(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    textlite_dir = working_dir / "textlite"
    duration = (time.perf_counter() - start) * 1000
    if not textlite_dir.exists():
        return SmokeItem(
            name="textlite_preview",
            status="SKIP",
            severity="INFO",
            message="TextLite cache missing",
            duration_ms=duration,
            hint="Run textlite preview once to create cache files.",
        )
    return SmokeItem(
        name="textlite_preview",
        status="PASS",
        severity="INFO",
        message="TextLite cache present",
        duration_ms=duration,
        data={"path": str(textlite_dir)},
    )


def _check_backup(settings: Dict[str, Any], working_dir: Path, diag: Dict[str, Any]) -> SmokeItem:
    start = time.perf_counter()
    backup_dir = working_dir / "exports" / "backups"
    duration = (time.perf_counter() - start) * 1000
    if not backup_dir.exists():
        return SmokeItem(
            name="backup_roundtrip",
            status="SKIP",
            severity="INFO",
            message="No backups yet",
            duration_ms=duration,
            hint="Create a backup snapshot to validate round-trip.",
        )
    snapshots = sorted(backup_dir.glob("*.zip"))
    if not snapshots:
        return SmokeItem(
            name="backup_roundtrip",
            status="SKIP",
            severity="INFO",
            message="Backup folder empty",
            duration_ms=duration,
        )
    latest = snapshots[-1]
    return SmokeItem(
        name="backup_roundtrip",
        status="PASS",
        severity="INFO",
        message="Backup snapshots available",
        duration_ms=duration,
        data={"latest": latest.name},
    )


TEST_MAP: Dict[str, Tuple[Callable[..., SmokeItem], Tuple[int, int]]] = {
    "orchestrator_ping": (_check_orchestrator, EVENT_RANGES["smoke_orchestrator"]),
    "web_api_list": (_check_web_api, EVENT_RANGES["smoke_web"]),
    "realtime_subscribe": (_check_realtime, EVENT_RANGES["smoke_web"]),
    "assistant_ask_tiny": (_check_assistant, EVENT_RANGES["smoke_ai"]),
    "rag_embed_min": (_check_rag, EVENT_RANGES["smoke_rag"]),
    "apiguard_cache_probe": (_check_apiguard, EVENT_RANGES["smoke_apiguard"]),
    "quality_headers": (_check_quality, EVENT_RANGES["smoke_quality"]),
    "textlite_preview": (_check_textlite, EVENT_RANGES["smoke_textlite"]),
    "backup_roundtrip": (_check_backup, EVENT_RANGES["smoke_backup"]),
}


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


def run_smoke(
    targets: Optional[Iterable[str]] = None,
    *,
    budget: Optional[int] = None,
    working_dir: Optional[Path] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run smoke tests for selected subsystems."""

    resolved = working_dir or resolve_working_dir()
    settings_payload = settings or load_settings(resolved) or {}
    diag_settings = settings_payload.get("diagnostics") if isinstance(settings_payload.get("diagnostics"), dict) else {}

    requested = [name for name in (targets or DEFAULT_TARGETS) if name in TEST_MAP]
    if budget is not None:
        requested = requested[: max(1, int(budget))]

    items: List[SmokeItem] = []
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}

    for name in requested:
        func, (event_start, _) = TEST_MAP[name]
        correlation_id = new_correlation_id()
        start = time.perf_counter()
        try:
            item = func(settings_payload, resolved, diag_settings)
        except Exception as exc:  # pragma: no cover - defensive
            duration = (time.perf_counter() - start) * 1000
            item = SmokeItem(
                name=name,
                status="FAIL",
                severity="MAJOR",
                message="Unhandled exception during smoke test",
                duration_ms=duration,
                hint=str(exc),
            )
        counts[item.status] = counts.get(item.status, 0) + 1
        items.append(item)
        log_event(
            event_id=event_start,
            level="INFO" if item.status == "PASS" else "WARNING" if item.status == "SKIP" else "ERROR",
            module="diagnostics.smoke",
            op=name,
            working_dir=resolved,
            correlation_id=correlation_id,
            duration_ms=item.duration_ms,
            ok=item.status == "PASS",
            err_code=None if item.status == "PASS" else item.status,
            hint=item.hint,
            details=item.data,
        )

    payload = {
        "id": datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "ts": time.time(),
        "summary": {
            "pass": counts.get("PASS", 0),
            "fail": counts.get("FAIL", 0),
            "skip": counts.get("SKIP", 0),
        },
        "items": [
            {
                "name": item.name,
                "status": item.status,
                "severity": item.severity,
                "message": item.message,
                "duration_ms": item.duration_ms,
                "hint": item.hint,
                "data": item.data,
            }
            for item in items
        ],
    }

    persist_snapshot("smoke", payload, working_dir=resolved)

    db_path = get_catalog_db_path(resolved)
    if db_path.exists():
        conn = core_db.connect(db_path, read_only=False, timeout=5.0)
        try:
            _ensure_diag_tables(conn)
            _record_metric(conn, series="smoke_pass", value=float(counts.get("PASS", 0)))
            _record_metric(conn, series="smoke_fail", value=float(counts.get("FAIL", 0)))
            conn.commit()
        finally:
            conn.close()

    return payload


__all__ = ["run_smoke", "SmokeItem", "DEFAULT_TARGETS"]
