"""Structured diagnostics logging for VideoCatalog."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

from core.paths import get_logs_dir, resolve_working_dir

LOG_FILENAME = "diagnostics.jsonl"
SNAPSHOT_DIRNAME = "diagnostics"
PREFLIGHT_SNAPSHOT = "preflight.json"
SMOKE_SNAPSHOT = "smoke.json"

EVENT_RANGES: Dict[str, tuple[int, int]] = {
    "preflight": (1000, 1099),
    "smoke_orchestrator": (1100, 1199),
    "smoke_web": (1200, 1299),
    "smoke_ai": (1300, 1399),
    "smoke_rag": (1400, 1499),
    "smoke_apiguard": (1500, 1599),
    "smoke_quality": (1600, 1699),
    "smoke_textlite": (1700, 1799),
    "smoke_backup": (1800, 1899),
    "report": (1900, 1999),
}


class DiagnosticsLogError(Exception):
    """Raised when diagnostics logs cannot be processed."""


def new_correlation_id() -> str:
    """Return a short correlation identifier for related log entries."""

    return uuid.uuid4().hex[:16]


def _ensure_logs_dir(working_dir: Optional[Path] = None) -> Path:
    base = working_dir or resolve_working_dir()
    logs_dir = get_logs_dir(base)
    logs_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = logs_dir / SNAPSHOT_DIRNAME
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def _log_path(working_dir: Optional[Path] = None) -> Path:
    return _ensure_logs_dir(working_dir) / LOG_FILENAME


def _snapshot_path(name: str, working_dir: Optional[Path] = None) -> Path:
    directory = _ensure_logs_dir(working_dir) / SNAPSHOT_DIRNAME
    filename = PREFLIGHT_SNAPSHOT if name == "preflight" else SMOKE_SNAPSHOT
    return directory / filename


def latest_preflight_path(working_dir: Optional[Path] = None) -> Path:
    return _snapshot_path("preflight", working_dir)


def latest_smoke_path(working_dir: Optional[Path] = None) -> Path:
    return _snapshot_path("smoke", working_dir)


def log_event(
    *,
    event_id: int,
    level: str,
    module: str,
    op: str,
    working_dir: Optional[Path] = None,
    correlation_id: Optional[str] = None,
    duration_ms: Optional[float] = None,
    ok: Optional[bool] = None,
    err_code: Optional[str] = None,
    hint: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a structured log entry using JSONL."""

    record: Dict[str, Any] = {
        "ts": time.time(),
        "level": (level or "INFO").upper(),
        "event_id": int(event_id),
        "module": module,
        "op": op,
    }
    if correlation_id:
        record["cid"] = correlation_id
    if duration_ms is not None:
        record["duration_ms"] = float(duration_ms)
    if ok is not None:
        record["ok"] = bool(ok)
    if err_code:
        record["err_code"] = str(err_code)
    if hint:
        record["hint"] = str(hint)
    if details:
        record["details"] = details

    path = _log_path(working_dir)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + os.linesep)


def persist_snapshot(name: str, payload: Dict[str, Any], *, working_dir: Optional[Path] = None) -> Path:
    """Persist the latest preflight/smoke snapshot for reuse."""

    path = _snapshot_path(name, working_dir)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def load_snapshot(name: str, *, working_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load a previously persisted snapshot if available."""

    path = _snapshot_path(name, working_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise DiagnosticsLogError(f"Snapshot {name} corrupted: {exc}") from exc
    if isinstance(payload, dict):
        return payload
    return None


def _iter_logs(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return iter(())
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return iter(())


def query_logs(
    *,
    working_dir: Optional[Path] = None,
    event_id: Optional[int] = None,
    module: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Return a filtered view of structured diagnostics logs."""

    path = _log_path(working_dir)
    records: list[Dict[str, Any]] = []
    level_norm = level.upper() if level else None
    module_norm = module.lower() if module else None
    for record in _iter_logs(path):
        if event_id is not None and int(record.get("event_id", -1)) != int(event_id):
            continue
        if level_norm and str(record.get("level", "")).upper() != level_norm:
            continue
        if module_norm and module_norm not in str(record.get("module", "")).lower():
            continue
        records.append(record)
    if limit > 0:
        records = records[-int(limit) :]
    return {"count": len(records), "rows": records}


def purge_old_logs(*, keep_days: int, working_dir: Optional[Path] = None) -> None:
    """Trim logs older than the configured retention."""

    if keep_days <= 0:
        return
    threshold = time.time() - keep_days * 86400
    path = _log_path(working_dir)
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return
    filtered = [row for row in rows if float(row.get("ts", 0)) >= threshold]
    with open(path, "w", encoding="utf-8") as handle:
        for row in filtered:
            handle.write(json.dumps(row, ensure_ascii=False) + os.linesep)


__all__ = [
    "DiagnosticsLogError",
    "EVENT_RANGES",
    "latest_preflight_path",
    "latest_smoke_path",
    "load_snapshot",
    "log_event",
    "new_correlation_id",
    "persist_snapshot",
    "purge_old_logs",
    "query_logs",
]
