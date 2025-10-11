from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence

from core import db as core_db
from core.paths import get_catalog_db_path, get_logs_dir, resolve_working_dir, to_long_path
from core.settings import load_settings

LOG_SAMPLE_COUNT = 200
_GUI_WATCHDOG_FILE = "gui_watchdog.json"
_CACHE_METRICS_FILE = "cache_metrics.json"
_API_STATE_FILE = "api_state.json"
_PATH_EVENTS_FILE = "path_events.jsonl"
_SLOW_QUERIES_FILE = "slow_queries.jsonl"


class HealthSeverity(str, Enum):
    MAJOR = "MAJOR"
    MINOR = "MINOR"


@dataclass(slots=True)
class HealthItem:
    severity: HealthSeverity
    code: str
    where: str
    hint: str
    details: Optional[str] = None


@dataclass(slots=True)
class HealthSummary:
    major: int = 0
    minor: int = 0

    @classmethod
    def from_items(cls, items: Sequence[HealthItem]) -> "HealthSummary":
        major = sum(1 for item in items if item.severity is HealthSeverity.MAJOR)
        minor = sum(1 for item in items if item.severity is HealthSeverity.MINOR)
        return cls(major=major, minor=minor)


@dataclass(slots=True)
class HealthReport:
    ts: float
    items: List[HealthItem]

    @property
    def summary(self) -> HealthSummary:
        return HealthSummary.from_items(self.items)


CheckFunc = Callable[[Path], Iterator[HealthItem]]


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _iter_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
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
        return


def _check_db_configuration(working_dir: Path) -> Iterator[HealthItem]:
    db_path = get_catalog_db_path(working_dir)
    if not db_path.exists():
        return iter(())

    try:
        conn = core_db.connect(db_path, read_only=True, timeout=2.0)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        return iter(
            [
                HealthItem(
                    severity=HealthSeverity.MAJOR,
                    code="DB_OPEN_FAIL",
                    where="core/db.py",
                    hint="Ensure catalog DB is accessible",
                    details=str(exc),
                )
            ]
        )

    items: list[HealthItem] = []
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
        if not journal_mode or str(journal_mode[0]).lower() != "wal":
            items.append(
                HealthItem(
                    severity=HealthSeverity.MAJOR,
                    code="DB_WAL_OFF",
                    where="core/db.py",
                    hint="Enable WAL & busy_timeout",
                )
            )
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()
        if not busy_timeout or int(busy_timeout[0]) < core_db.DEFAULT_BUSY_TIMEOUT_MS:
            items.append(
                HealthItem(
                    severity=HealthSeverity.MAJOR,
                    code="DB_BUSY_LOW",
                    where="core/db.py",
                    hint="Increase PRAGMA busy_timeout to >= DEFAULT_BUSY_TIMEOUT_MS",
                )
            )

        try:
            wal_file = db_path.with_suffix(".db-wal")
            if wal_file.exists() and wal_file.stat().st_size > 64 * 1024 * 1024:
                items.append(
                    HealthItem(
                        severity=HealthSeverity.MINOR,
                        code="DB_WAL_SIZE",
                        where=str(db_path),
                        hint="Checkpoint WAL to keep it under control",
                    )
                )
        except OSError:
            pass
    finally:
        conn.close()

    return iter(items)


def _check_long_path_usage(working_dir: Path) -> Iterator[HealthItem]:
    if os.name != "nt":
        return iter(())
    items: list[HealthItem] = []
    catalog = get_catalog_db_path(working_dir)
    normalized = to_long_path(catalog)
    if len(str(catalog)) >= 240 and not normalized.startswith("\\\\?\\"):
        items.append(
            HealthItem(
                severity=HealthSeverity.MAJOR,
                code="PATH_LONG_UNSAFE",
                where="core/paths.py",
                hint="Normalize catalog path with to_long_path before IO",
            )
        )
    events_path = get_logs_dir(working_dir) / _PATH_EVENTS_FILE
    for payload in list(_iter_jsonl(events_path))[-LOG_SAMPLE_COUNT:]:
        try:
            severity = payload.get("severity", "MAJOR").upper()
            code = payload.get("code", "PATH_UNSAFE")
            details = payload.get("details")
            items.append(
                HealthItem(
                    severity=HealthSeverity(severity),
                    code=code,
                    where=payload.get("where", "core/paths.py"),
                    hint=payload.get("hint", "Normalize paths via to_long_path"),
                    details=details,
                )
            )
        except Exception:
            continue
    return iter(items)


def _check_tooling(working_dir: Path) -> Iterator[HealthItem]:
    required = {
        "ffprobe": "Install ffmpeg/ffprobe or configure manual path",
        "tmk": "Install Facebook TMK binaries and configure fingerprints",
        "fpcalc": "Install Chromaprint tools (fpcalc)",
    }
    missing: list[HealthItem] = []
    settings = load_settings(working_dir) or {}
    manual_paths = {
        "ffprobe": settings.get("quality", {}).get("ffprobe_path"),
        "tmk": settings.get("fingerprints", {}).get("tmk_bin_path"),
        "fpcalc": settings.get("fingerprints", {}).get("fpcalc_path"),
    }
    for tool, hint in required.items():
        path = manual_paths.get(tool)
        if path:
            candidate = Path(path)
            if candidate.exists():
                continue
        if shutil.which(tool) is None:
            missing.append(
                HealthItem(
                    severity=HealthSeverity.MAJOR,
                    code=f"TOOL_{tool.upper()}_MISSING",
                    where="tools.py",
                    hint=hint,
                )
            )
    return iter(missing)


def _check_gpu_provider(working_dir: Path) -> Iterator[HealthItem]:
    logs_dir = get_logs_dir(working_dir)
    state_path = logs_dir / "gpu_state.json"
    payload = _read_json(state_path)
    if not payload:
        return iter(())
    if payload.get("status") == "error":
        return iter(
            [
                HealthItem(
                    severity=HealthSeverity.MAJOR,
                    code="GPU_INIT_FAIL",
                    where=payload.get("where", "gpu/runtime.py"),
                    hint=payload.get("hint", "Fallback to CPU provider"),
                    details=payload.get("message"),
                )
            ]
        )
    free_vram = payload.get("free_vram_mb")
    min_required = payload.get("min_required_mb")
    if isinstance(free_vram, (int, float)) and isinstance(min_required, (int, float)):
        if free_vram < min_required:
            return iter(
                [
                    HealthItem(
                        severity=HealthSeverity.MINOR,
                        code="GPU_LOW_VRAM",
                        where="gpu/runtime.py",
                        hint="GPU memory guard engaged; running on CPU",
                    )
                ]
            )
    return iter(())


def _check_gui_watchdog(working_dir: Path) -> Iterator[HealthItem]:
    payload = _read_json(get_logs_dir(working_dir) / _GUI_WATCHDOG_FILE)
    if not payload:
        return iter(())
    max_block_ms = payload.get("max_block_ms", 0)
    if max_block_ms and max_block_ms > 200:
        return iter(
            [
                HealthItem(
                    severity=HealthSeverity.MAJOR,
                    code="GUI_THREAD_BLOCK",
                    where=payload.get("where", "ui/watchdog.py"),
                    hint="Move blocking call to background worker",
                    details=f"Observed {max_block_ms:.0f} ms stall",
                )
            ]
        )
    return iter(())


def _check_api_quota(working_dir: Path) -> Iterator[HealthItem]:
    payload = _read_json(get_logs_dir(working_dir) / _API_STATE_FILE)
    if not payload:
        return iter(())
    remaining = payload.get("remaining")
    limit = payload.get("limit")
    if isinstance(remaining, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
        ratio = remaining / limit
        if ratio <= 0.1:
            return iter(
                [
                    HealthItem(
                        severity=HealthSeverity.MINOR,
                        code="API_QUOTA_LOW",
                        where=payload.get("where", "apiguard/client.py"),
                        hint=payload.get("hint", "Reduce API usage or request more quota"),
                        details=f"{remaining}/{limit} left",
                    )
                ]
            )
    return iter(())


def _check_cache_metrics(working_dir: Path) -> Iterator[HealthItem]:
    payload = _read_json(get_logs_dir(working_dir) / _CACHE_METRICS_FILE)
    if not payload:
        return iter(())
    stale = payload.get("stale_entries")
    total = payload.get("total_entries")
    if isinstance(stale, int) and isinstance(total, int) and total > 0:
        ratio = stale / total
        if ratio >= 0.4:
            return iter(
                [
                    HealthItem(
                        severity=HealthSeverity.MINOR,
                        code="CACHE_STALE_HIGH",
                        where=payload.get("where", "core/cache.py"),
                        hint=payload.get("hint", "Warm cache or clear stale entries"),
                        details=f"stale ratio {ratio:.2f}",
                    )
                ]
            )
    return iter(())


def _check_slow_queries(working_dir: Path) -> Iterator[HealthItem]:
    slow_entries = list(_iter_jsonl(get_logs_dir(working_dir) / _SLOW_QUERIES_FILE))
    if not slow_entries:
        return iter(())
    offenders: list[HealthItem] = []
    for entry in slow_entries[-LOG_SAMPLE_COUNT:]:
        duration_ms = entry.get("duration_ms")
        if duration_ms and duration_ms > 300:
            offenders.append(
                HealthItem(
                    severity=HealthSeverity.MINOR,
                    code="DB_SLOW_QUERY",
                    where=entry.get("where", "core/db.py"),
                    hint=entry.get("hint", "Consider adding an index"),
                    details=f"{duration_ms:.0f} ms: {entry.get('sql', '')[:80]}",
                )
            )
    return iter(offenders)


def _check_settings_schema(working_dir: Path) -> Iterator[HealthItem]:
    from core.settings_schema import SETTINGS_VALIDATOR

    settings = load_settings(working_dir) or {}
    unknown_keys = SETTINGS_VALIDATOR.unknown_keys(settings)
    items: list[HealthItem] = []
    if unknown_keys:
        items.append(
            HealthItem(
                severity=HealthSeverity.MINOR,
                code="SETTINGS_UNKNOWN",
                where="core/settings.py",
                hint="Remove unknown keys or update schema",
                details=", ".join(sorted(unknown_keys))[:120],
            )
        )
    return iter(items)


CHECKS: Sequence[CheckFunc] = (
    _check_db_configuration,
    _check_long_path_usage,
    _check_tooling,
    _check_gpu_provider,
    _check_gui_watchdog,
    _check_api_quota,
    _check_cache_metrics,
    _check_slow_queries,
    _check_settings_schema,
)


def run_checks(working_dir: Optional[Path] = None) -> HealthReport:
    base = working_dir or resolve_working_dir()
    items: list[HealthItem] = []
    for check in CHECKS:
        try:
            items.extend(list(check(base)))
        except Exception as exc:  # pragma: no cover - defensive
            items.append(
                HealthItem(
                    severity=HealthSeverity.MAJOR,
                    code="CHECK_FAIL",
                    where=getattr(check, "__name__", "health.check"),
                    hint="Check raised unexpectedly; inspect logs",
                    details=str(exc),
                )
            )
    return HealthReport(ts=time.time(), items=items)
