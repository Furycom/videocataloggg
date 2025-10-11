"""Structured diagnostics logging helpers."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

from core.paths import get_logs_dir, resolve_working_dir

LOG_FILENAME = "diagnostics.log.jsonl"
PREFLIGHT_SNAPSHOT = "diagnostics_preflight.json"
SMOKE_SNAPSHOT = "diagnostics_smoke.json"

EVENT_RANGES = {
    "preflight": (1000, 1999),
    "smoke": (2000, 2999),
    "apiguard": (3000, 3999),
    "assistant": (4000, 4999),
}


class DiagnosticsLogError(Exception):
    """Raised when diagnostics logs cannot be processed."""


def _ensure_logs_dir(working_dir: Optional[Path] = None) -> Path:
    base = working_dir or resolve_working_dir()
    logs_dir = get_logs_dir(base)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def _log_path(working_dir: Optional[Path] = None) -> Path:
    return _ensure_logs_dir(working_dir) / LOG_FILENAME


def _snapshot_path(filename: str, working_dir: Optional[Path] = None) -> Path:
    return _ensure_logs_dir(working_dir) / filename


def latest_preflight_path(working_dir: Optional[Path] = None) -> Path:
    return _snapshot_path(PREFLIGHT_SNAPSHOT, working_dir)


def latest_smoke_path(working_dir: Optional[Path] = None) -> Path:
    return _snapshot_path(SMOKE_SNAPSHOT, working_dir)


def log_event(
    *,
    event_id: int,
    level: str,
    module: str,
    op: str,
    working_dir: Optional[Path] = None,
    duration_ms: Optional[float] = None,
    ok: Optional[bool] = None,
    err_code: Optional[str] = None,
    hint: Optional[str] = None,
    path: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a structured diagnostics log entry."""

    record = {
        "ts": time.time(),
        "level": (level or "INFO").upper(),
        "event_id": int(event_id),
        "module": module,
        "op": op,
    }
    if duration_ms is not None:
        record["duration_ms"] = float(duration_ms)
    if ok is not None:
        record["ok"] = bool(ok)
    if err_code:
        record["err_code"] = str(err_code)
    if hint:
        record["hint"] = str(hint)
    if path:
        record["path"] = str(path)
    if extra:
        record.update(extra)
    log_path = _log_path(working_dir)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + os.linesep)


def persist_snapshot(name: str, payload: Dict[str, Any], *, working_dir: Optional[Path] = None) -> Path:
    """Persist the latest preflight/smoke snapshot for reuse."""

    filename = PREFLIGHT_SNAPSHOT if name == "preflight" else SMOKE_SNAPSHOT
    path = _snapshot_path(filename, working_dir)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def load_snapshot(name: str, *, working_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    filename = PREFLIGHT_SNAPSHOT if name == "preflight" else SMOKE_SNAPSHOT
    path = _snapshot_path(filename, working_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise DiagnosticsLogError(f"Snapshot {filename} corrupted: {exc}") from exc
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
    query: Optional[Dict[str, Any]] = None,
    limit: int = 200,
    working_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return the latest log lines filtered by event_id/module/level."""

    log_path = _log_path(working_dir)
    filters = dict(query or {})
    level = filters.get("level")
    module_filter = filters.get("module")
    event_id_filter = filters.get("event_id")
    matched: list[Dict[str, Any]] = []
    for record in _iter_logs(log_path):
        if level and str(record.get("level", "")).upper() != str(level).upper():
            continue
        if module_filter and module_filter not in str(record.get("module", "")):
            continue
        if event_id_filter is not None:
            try:
                event_id_value = int(event_id_filter)
            except (TypeError, ValueError):
                event_id_value = None
            if event_id_value is not None and int(record.get("event_id", -1)) != event_id_value:
                continue
        matched.append(record)
    matched = matched[-int(limit) :]
    return {"rows": matched, "count": len(matched)}


def purge_old_logs(*, keep_days: int, working_dir: Optional[Path] = None) -> None:
    """Trim diagnostics logs older than *keep_days*."""

    if keep_days <= 0:
        return
    threshold = time.time() - keep_days * 86400
    log_path = _log_path(working_dir)
    if not log_path.exists():
        return
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return
    filtered = [row for row in rows if float(row.get("ts", 0)) >= threshold]
    with open(log_path, "w", encoding="utf-8") as handle:
        for row in filtered:
            handle.write(json.dumps(row, ensure_ascii=False) + os.linesep)


__all__ = [
    "EVENT_RANGES",
    "DiagnosticsLogError",
    "log_event",
    "query_logs",
    "purge_old_logs",
    "persist_snapshot",
    "load_snapshot",
    "latest_preflight_path",
    "latest_smoke_path",
]
