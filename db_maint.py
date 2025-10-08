"""SQLite maintenance utilities for VideoCatalog."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from paths import (
    get_shards_dir,
    load_settings,
    resolve_working_dir,
    safe_label,
    save_settings,
)


LOGGER = logging.getLogger("videocatalog.dbmaint")


DEFAULT_VACUUM_THRESHOLD = 64 * 1024 * 1024  # 64 MB
DEFAULT_BUSY_TIMEOUT_MS = 5000
BACKUP_SUBDIR = Path("backups") / "sqlite"


PathLike = Union[str, os.PathLike[str]]


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _timestamp() -> str:
    return _now_utc().strftime("%Y%m%d_%H%M%SZ")


def _total_db_size(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}") if suffix else path
        if candidate.exists():
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _normalize_path(path: PathLike) -> Path:
    return Path(path).expanduser().resolve()


def database_size_bytes(db_path: PathLike) -> int:
    """Return the combined size of the SQLite DB and its sidecar files."""
    return _total_db_size(_normalize_path(db_path))


def _busy_timeout(conn: sqlite3.Connection, timeout_ms: int) -> None:
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    except sqlite3.Error:
        pass


def _fetch_int(conn: sqlite3.Connection, pragma: str) -> int:
    try:
        row = conn.execute(pragma).fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except sqlite3.Error:
        pass
    return 0


@dataclass(frozen=True)
class MaintenanceOptions:
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    vacuum_free_bytes_min: int = DEFAULT_VACUUM_THRESHOLD
    auto_vacuum_after_scan: bool = False


def resolve_options(settings: Optional[Dict[str, object]] = None) -> MaintenanceOptions:
    data = settings or {}
    raw = data.get("maintenance") if isinstance(data, dict) else None
    values: Dict[str, object] = {}
    if isinstance(raw, dict):
        values = dict(raw)
    busy_ms = int(values.get("busy_timeout_ms", DEFAULT_BUSY_TIMEOUT_MS))
    threshold = int(values.get("vacuum_free_bytes_min", DEFAULT_VACUUM_THRESHOLD))
    auto_vacuum = bool(values.get("auto_vacuum_after_scan", False))
    return MaintenanceOptions(
        busy_timeout_ms=busy_ms,
        vacuum_free_bytes_min=max(0, threshold),
        auto_vacuum_after_scan=auto_vacuum,
    )


def update_maintenance_metadata(
    *,
    last_run: Optional[datetime] = None,
    last_integrity_ok: Optional[bool] = None,
    working_dir: Optional[PathLike] = None,
) -> None:
    base = resolve_working_dir() if working_dir is None else Path(working_dir)
    settings = load_settings(base)
    maint = settings.get("maintenance") if isinstance(settings, dict) else None
    if not isinstance(maint, dict):
        maint = {}
    if last_run is not None:
        maint["last_run_utc"] = last_run.strftime("%Y-%m-%dT%H:%M:%SZ")
    if last_integrity_ok is not None:
        maint["last_integrity_ok"] = bool(last_integrity_ok)
    maint.setdefault("auto_vacuum_after_scan", False)
    maint.setdefault("vacuum_free_bytes_min", DEFAULT_VACUUM_THRESHOLD)
    maint.setdefault("busy_timeout_ms", DEFAULT_BUSY_TIMEOUT_MS)
    settings["maintenance"] = maint
    save_settings(settings, base)


def check_integrity(
    db_path: PathLike,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Dict[str, object]:
    path = _normalize_path(db_path)
    start = time.perf_counter()
    ok = True
    issues: List[str] = []
    total_issues = 0
    try:
        with sqlite3.connect(str(path)) as conn:
            _busy_timeout(conn, busy_timeout_ms)
            rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
            if rows and not all(row.lower() == "ok" for row in rows):
                rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
            normalized = [row.strip() for row in rows if isinstance(row, str) and row.strip()]
            if not normalized:
                ok = True
            else:
                ok = all(item.lower() == "ok" for item in normalized)
                if not ok:
                    total_issues = len(normalized)
                    issues = normalized[:10]
    except sqlite3.Error as exc:
        ok = False
        issues = [f"sqlite-error: {exc}"]
        total_issues = len(issues)
    duration = time.perf_counter() - start
    status_text = "ok" if ok else f"{total_issues} issue(s)"
    LOGGER.info("Integrity check %s — %s (%.2fs)", path.name, status_text, duration)
    if issues:
        for sample in issues[:10]:
            LOGGER.warning("Integrity sample %s: %s", path.name, sample)
    return {
        "ok": ok,
        "issues": issues,
        "total_issues": total_issues,
        "duration_s": duration,
        "path": str(path),
    }


def quick_optimize(
    db_path: PathLike,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Dict[str, object]:
    path = _normalize_path(db_path)
    start = time.perf_counter()
    with sqlite3.connect(str(path)) as conn:
        _busy_timeout(conn, busy_timeout_ms)
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
        conn.execute("PRAGMA optimize")
    duration = time.perf_counter() - start
    LOGGER.info("Quick optimize %s completed in %.2fs", path.name, duration)
    return {"duration_s": duration, "path": str(path)}


def reindex_and_analyze(
    db_path: PathLike,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Dict[str, object]:
    path = _normalize_path(db_path)
    start = time.perf_counter()
    indexes_before = 0
    indexes_after = 0
    with sqlite3.connect(str(path)) as conn:
        _busy_timeout(conn, busy_timeout_ms)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'")
            indexes_before = int(cur.fetchone()[0])
        except (sqlite3.Error, TypeError):
            indexes_before = 0
        conn.execute("REINDEX")
        conn.execute("ANALYZE")
        conn.execute("PRAGMA optimize")
        conn.commit()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'")
            indexes_after = int(cur.fetchone()[0])
        except (sqlite3.Error, TypeError):
            indexes_after = indexes_before
    duration = time.perf_counter() - start
    LOGGER.info(
        "Reindex/Analyze %s — indexes=%s (was %s) in %.2fs",
        path.name,
        indexes_after,
        indexes_before,
        duration,
    )
    return {
        "indexes_before": indexes_before,
        "indexes_after": indexes_after,
        "duration_s": duration,
        "path": str(path),
    }


def vacuum_if_needed(
    db_path: PathLike,
    *,
    thresholds: Optional[Dict[str, object]] = None,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    force: bool = False,
    active_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, object]:
    path = _normalize_path(db_path)
    threshold_bytes = DEFAULT_VACUUM_THRESHOLD
    if isinstance(thresholds, dict):
        value = thresholds.get("vacuum_free_bytes_min")
        if value is None:
            value = thresholds.get("free_bytes_min")
        if value is not None:
            try:
                threshold_bytes = int(value)
            except (TypeError, ValueError):
                threshold_bytes = DEFAULT_VACUUM_THRESHOLD
    before_bytes = _total_db_size(path)
    start = time.perf_counter()
    with sqlite3.connect(str(path)) as conn:
        _busy_timeout(conn, busy_timeout_ms)
        journal_mode = ""
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            if row and row[0]:
                journal_mode = str(row[0]).lower()
        except sqlite3.Error:
            journal_mode = ""
        if journal_mode == "wal":
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        page_count = _fetch_int(conn, "PRAGMA page_count")
        page_size = max(1, _fetch_int(conn, "PRAGMA page_size") or 4096)
        freelist = _fetch_int(conn, "PRAGMA freelist_count")
        allocated_bytes = page_count * page_size
        freelist_bytes = freelist * page_size
        free_estimate = max(freelist_bytes, max(0, allocated_bytes - before_bytes))
        if not force and free_estimate < threshold_bytes:
            LOGGER.info(
                "VACUUM skipped for %s — free=%s bytes < threshold=%s bytes",
                path.name,
                free_estimate,
                threshold_bytes,
            )
            return {
                "skipped": True,
                "reason": "threshold",
                "free_bytes": free_estimate,
                "threshold": threshold_bytes,
                "path": str(path),
            }
        if active_check and active_check():
            raise RuntimeError("Active scan detected for this database; VACUUM aborted.")
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    after_bytes = _total_db_size(path)
    reclaimed = max(0, before_bytes - after_bytes)
    duration = time.perf_counter() - start
    LOGGER.info(
        "VACUUM %s reclaimed %s bytes in %.2fs",
        path.name,
        reclaimed,
        duration,
    )
    return {
        "skipped": False,
        "reclaimed_bytes": reclaimed,
        "duration_s": duration,
        "path": str(path),
        "free_bytes": free_estimate,
        "threshold": threshold_bytes,
    }


def light_backup(
    db_path: PathLike,
    label: str,
    *,
    working_dir: Optional[PathLike] = None,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Path:
    path = _normalize_path(db_path)
    base_dir = resolve_working_dir() if working_dir is None else Path(working_dir)
    target_dir = base_dir / BACKUP_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    normalized_label = safe_label(label) or "database"
    backup_path = target_dir / f"{_timestamp()}_{normalized_label}.db.bak"
    with sqlite3.connect(str(path)) as source:
        _busy_timeout(source, busy_timeout_ms)
        try:
            source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        with sqlite3.connect(str(backup_path)) as dest:
            source.backup(dest)
    LOGGER.info("Backup %s → %s", path.name, backup_path)
    return backup_path


ShardSelection = Union[str, Sequence[str], PathLike]


def _iter_shard_paths(
    selection: ShardSelection,
    *,
    working_dir: Optional[PathLike] = None,
) -> Iterator[Tuple[str, Path]]:
    base = resolve_working_dir() if working_dir is None else Path(working_dir)
    shards_dir = get_shards_dir(base)
    shards_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(selection, (str, os.PathLike)):
        text = str(selection)
        lower = text.lower()
        if lower in {"all", "all-shards"}:
            names = sorted(p.stem for p in shards_dir.glob("*.db"))
            for name in names:
                yield name, shards_dir / f"{name}.db"
            return
        if lower.startswith("shard:"):
            label = text.split(":", 1)[1]
            yield safe_label(label) or "drive", shards_dir / f"{safe_label(label)}.db"
            return
        candidate_path = Path(text)
        if candidate_path.exists():
            yield candidate_path.stem, candidate_path
            return
        label = safe_label(text) or "drive"
        yield label, shards_dir / f"{label}.db"
        return

    if isinstance(selection, Sequence):
        for entry in selection:
            if isinstance(entry, (str, os.PathLike)):
                label = safe_label(str(entry)) or "drive"
                yield label, shards_dir / f"{label}.db"
        return

    for db_path in sorted(shards_dir.glob("*.db")):
        yield db_path.stem, db_path


def for_each_shard(
    apply_fn: Callable[[str, Path], object],
    *,
    working_dir: Optional[PathLike] = None,
    selection: ShardSelection = "all",
    progress: Optional[Callable[[int, int, str, Path], None]] = None,
) -> List[Tuple[str, object]]:
    items = list(_iter_shard_paths(selection, working_dir=working_dir))
    total = len(items)
    results: List[Tuple[str, object]] = []
    for index, (label, path) in enumerate(items, start=1):
        if progress:
            progress(index, total, label, path)
        result = apply_fn(label, path)
        results.append((label, result))
    return results

