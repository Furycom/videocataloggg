import argparse
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import queue
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

from paths import (
    ensure_working_dir_structure,
    get_catalog_db_path,
    get_shard_db_path,
    load_settings,
    resolve_working_dir,
)
from exports import ExportFilters, export_catalog, parse_since
from tools import bootstrap_local_bin, probe_tool

from perf import (
    RateController,
    enumerate_sleep_range,
    resolve_performance_config,
)
from robust import (
    CancellationToken,
    PathTooLongError,
    clamp_batch_seconds,
    from_fs_path,
    is_hidden,
    is_transient,
    key_for_path,
    merge_settings,
    should_ignore,
    to_fs_path,
)

WORKING_DIR_PATH = resolve_working_dir()
ensure_working_dir_structure(WORKING_DIR_PATH)
bootstrap_local_bin(WORKING_DIR_PATH)
DEFAULT_DB_PATH = str(get_catalog_db_path(WORKING_DIR_PATH))
LOGGER = logging.getLogger("videocatalog.scan")

ALL_TOOLS = ("mediainfo", "ffmpeg", "smartctl")
REQUIRED_TOOLS = ("mediainfo", "ffmpeg")
TOOL_STATUS: Dict[str, dict] = {}
TOOL_PATHS: Dict[str, Optional[str]] = {}


def _refresh_tool_status() -> Dict[str, dict]:
    global TOOL_STATUS, TOOL_PATHS
    statuses: Dict[str, dict] = {}
    for tool in ALL_TOOLS:
        statuses[tool] = probe_tool(tool, WORKING_DIR_PATH)
    TOOL_STATUS = statuses
    TOOL_PATHS = {
        tool: (info.get("path") if info.get("present") else None)
        for tool, info in statuses.items()
    }
    return statuses


def _expand_user_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded)

_tqdm_spec = importlib.util.find_spec("tqdm")
if _tqdm_spec is None:
    def tqdm(iterable, **kwargs):
        return iterable
else:
    tqdm = importlib.import_module("tqdm").tqdm

_blake3_spec = importlib.util.find_spec("blake3")
_blake3_hash = importlib.import_module("blake3").blake3 if _blake3_spec is not None else None

VIDEO_EXTS = {'.mp4','.mkv','.avi','.mov','.wmv','.m4v','.ts','.m2ts','.webm','.mpg','.mpeg'}
AUDIO_EXTS = {'.mp3','.flac','.aac','.m4a','.wav','.wma','.ogg','.opus','.alac','.aiff','.ape','.dsf','.dff'}
AV_EXTS = VIDEO_EXTS | AUDIO_EXTS


@dataclass(slots=True)
class FileInfo:
    path: str  # original display/DB path
    fs_path: str  # path used for filesystem operations (may include long-path prefix)
    size_bytes: int
    mtime_utc: str
    is_av: bool
    existing_id: Optional[int] = None
    was_deleted: bool = False


@dataclass(slots=True)
class WorkerResult:
    info: FileInfo
    hash_value: Optional[str]
    media_blob: Optional[str]
    integrity_ok: Optional[int]
    error_message: Optional[str] = None


class ScanStateStore:
    def __init__(self, conn: sqlite3.Connection, drive_label: str, interval_seconds: int = 5):
        self.conn = conn
        self.drive_label = drive_label
        self.interval_seconds = max(1, int(interval_seconds))
        self._last_checkpoint = 0.0

    def _key(self, name: str) -> str:
        return f"{self.drive_label}::{name}"

    def load(self) -> Dict[str, str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT key, value FROM scan_state WHERE key LIKE ?",
            (f"{self.drive_label}::%",),
        )
        rows = cur.fetchall()
        state: Dict[str, str] = {}
        for key, value in rows:
            suffix = key.split("::", 1)[1] if "::" in key else key
            state[suffix] = value
        return state

    def clear(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM scan_state WHERE key LIKE ?",
            (f"{self.drive_label}::%",),
        )
        self.conn.commit()
        self._last_checkpoint = 0.0

    def checkpoint(self, phase: str, last_path: Optional[str], *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_checkpoint) < self.interval_seconds:
            return
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self.conn.cursor()
        rows = [
            (self._key("phase"), phase),
            (self._key("timestamp"), timestamp),
        ]
        if last_path is not None:
            rows.append((self._key("last_path_processed"), last_path))
        cur.executemany(
            "INSERT INTO scan_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            rows,
        )
        self.conn.commit()
        self._last_checkpoint = now


def run(cmd:list[str]) -> tuple[int,str,str]:
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
        out, err = p.communicate()
        return p.returncode, out, err
    except FileNotFoundError as exc:
        # Mirror the return signature while preventing the whole scan from
        # crashing when optional CLI dependencies (mediainfo/smartctl/ffmpeg)
        # are not installed.
        return 127, "", str(exc)

def mediainfo_json(file_path: str, executable: Optional[str]) -> Optional[dict]:
    if not executable:
        return None
    code, out, err = run([executable, "--Output=JSON", file_path])
    if code == 0 and out.strip():
        try:
            return json.loads(out)
        except Exception:
            return None
    return None

def ffmpeg_verify(file_path: str, executable: Optional[str]) -> bool:
    if not any(file_path.lower().endswith(e) for e in VIDEO_EXTS):
        return True
    if not executable:
        return True
    # -v error: only show errors; -xerror: stop on error
    code, out, err = run([executable, "-v", "error", "-xerror", "-i", file_path, "-f", "null", "-", "-nostdin"])
    return code == 0

def hash_blake3(
    file_path: str,
    chunk: int = 1024 * 1024,
    *,
    on_chunk: Optional[Callable[[int, float], None]] = None,
) -> str:
    if _blake3_hash is None:
        h = hashlib.sha256()
    else:
        h = _blake3_hash()
    with open(file_path, "rb") as f:
        while True:
            start = time.perf_counter()
            b = f.read(chunk)
            elapsed = time.perf_counter() - start
            if not b:
                if on_chunk is not None and elapsed > 0:
                    on_chunk(0, elapsed)
                break
            h.update(b)
            if on_chunk is not None:
                on_chunk(len(b), elapsed)
    return h.hexdigest()


def _iso_from_timestamp(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_av(path: str) -> bool:
    return Path(path).suffix.lower() in AV_EXTS


def _load_existing(
    conn: sqlite3.Connection, drive_label: str, *, casefold: bool
) -> Dict[str, dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, path, size_bytes, mtime_utc, deleted FROM files WHERE drive_label=?",
        (drive_label,),
    )
    existing: Dict[str, dict] = {}
    for row in cur.fetchall():
        file_id, path, size_bytes, mtime_utc, deleted = row
        key = key_for_path(path, casefold=casefold)
        existing[key] = {
            "id": file_id,
            "path": path,
            "size_bytes": size_bytes,
            "mtime_utc": mtime_utc,
            "deleted": int(deleted or 0),
        }
    return existing


def _mark_deleted(
    conn: sqlite3.Connection,
    drive_label: str,
    *,
    deleted_paths: Iterable[str],
) -> Tuple[int, List[str]]:
    cur = conn.cursor()
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    deleted_paths = list(deleted_paths)
    if not deleted_paths:
        return 0, []
    cur.executemany(
        "UPDATE files SET deleted=1, deleted_ts=? WHERE drive_label=? AND path=? AND deleted=0",
        [(timestamp, drive_label, path) for path in deleted_paths],
    )
    conn.commit()
    return cur.rowcount, deleted_paths[:5]


def _restore_active(conn: sqlite3.Connection, drive_label: str, paths: Iterable[str]) -> None:
    batch = [(drive_label, path) for path in paths]
    if not batch:
        return
    cur = conn.cursor()
    cur.executemany(
        "UPDATE files SET deleted=0, deleted_ts=NULL WHERE drive_label=? AND path=?",
        batch,
    )
    conn.commit()


def _upsert_file(
    conn: sqlite3.Connection,
    drive_label: str,
    info: FileInfo,
    *,
    hash_value: Optional[str],
    media_blob: Optional[str],
    integrity_ok: Optional[int],
) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM files WHERE drive_label=? AND path=?",
        (drive_label, info.path),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            """
            UPDATE files
            SET size_bytes=?, hash_blake3=?, media_json=?, integrity_ok=?, mtime_utc=?, deleted=0, deleted_ts=NULL
            WHERE id=?
            """,
            (
                int(info.size_bytes),
                hash_value,
                media_blob,
                integrity_ok,
                info.mtime_utc,
                int(row[0]),
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO files(
                drive_label, path, size_bytes, hash_blake3, media_json, integrity_ok, mtime_utc, deleted, deleted_ts
            )
            VALUES(?,?,?,?,?,?,?,?,NULL)
            """,
            (
                drive_label,
                info.path,
                int(info.size_bytes),
                hash_value,
                media_blob,
                integrity_ok,
                info.mtime_utc,
                0,
            ),
        )
    conn.commit()

def init_db(db_path: str):
    db_path_obj = Path(db_path)
    db_path_obj.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path_obj))
    c = conn.cursor()
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS drives(
        id INTEGER PRIMARY KEY,
        label TEXT NOT NULL,
        mount_path TEXT NOT NULL,
        total_bytes INTEGER,
        free_bytes INTEGER,
        smart_scan TEXT,      -- raw smartctl --scan/--xall best-effort
        scanned_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY,
        drive_label TEXT NOT NULL,
        path TEXT NOT NULL,
        size_bytes INTEGER,
        hash_blake3 TEXT,
        media_json TEXT,
        integrity_ok INTEGER,
        mtime_utc TEXT,
        deleted INTEGER DEFAULT 0,
        deleted_ts TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_files_drive ON files(drive_label);
    CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash_blake3);
    CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
    CREATE TABLE IF NOT EXISTS scan_state(
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_drives_label_unique ON drives(label);
    """)
    conn.commit()
    # Migrations for legacy shards
    c.execute("PRAGMA table_info(files)")
    existing_cols = {row[1] for row in c.fetchall()}
    for name, ddl in (
        ("deleted", "ALTER TABLE files ADD COLUMN deleted INTEGER DEFAULT 0"),
        ("deleted_ts", "ALTER TABLE files ADD COLUMN deleted_ts TEXT"),
    ):
        if name not in existing_cols:
            c.execute(ddl)
    conn.commit()
    return conn

def try_smart_overview(executable: Optional[str]) -> Optional[str]:
    # Best-effort: capture smartctl --scan-open and a subset of PhysicalDrive outputs
    if not executable:
        return None
    acc = {"scan": None, "details": []}
    code, out, err = run([executable, "--scan-open"])
    if code == 0:
        acc["scan"] = out
    for n in range(0, 10):
        code, out, err = run([executable, "-i", "-H", "-A", "-j", fr"\\.\PhysicalDrive{n}"])
        if code == 0 and out.strip():
            acc["details"].append(json.loads(out))
    return json.dumps(acc, ensure_ascii=False)

def scan_drive(
    label: str,
    mount_path: str,
    db_path: str,
    *,
    full_rescan: bool = False,
    resume: bool = True,
    checkpoint_seconds: int = 5,
    debug_slow: bool = False,
    settings: Optional[Dict[str, object]] = None,
    perf_overrides: Optional[Dict[str, object]] = None,
    robust_overrides: Optional[Dict[str, object]] = None,
) -> dict:
    mount = Path(mount_path)
    if not mount.exists():
        print(f"[ERROR] Mount path not found: {mount_path}")
        sys.exit(2)

    effective_settings = settings or load_settings(WORKING_DIR_PATH)
    perf_overrides = perf_overrides or {}
    perf_config = resolve_performance_config(
        str(mount),
        settings=effective_settings,
        cli_overrides=perf_overrides,
    )
    LOGGER.info(
        "Perf: profile=%s threads=%s chunk=%s ffmpeg_parallel=%s gentle_io=%s",
        perf_config.profile,
        perf_config.worker_threads,
        perf_config.hash_chunk_bytes,
        perf_config.ffmpeg_parallel,
        str(bool(perf_config.gentle_io)).lower(),
    )
    try:
        print(
            json.dumps(
                {
                    "type": "performance",
                    "profile": perf_config.profile,
                    "auto_profile": perf_config.auto_profile,
                    "source": perf_config.source,
                    "worker_threads": perf_config.worker_threads,
                    "hash_chunk_bytes": perf_config.hash_chunk_bytes,
                    "ffmpeg_parallel": perf_config.ffmpeg_parallel,
                    "gentle_io": bool(perf_config.gentle_io),
                }
            ),
            flush=True,
        )
    except Exception:
        pass

    LOGGER.info(
        "Delta rescan = %s, resume = %s, checkpoint = %ss",
        "false" if full_rescan else "true",
        "true" if resume else "false",
        int(checkpoint_seconds),
    )

    start_time = time.perf_counter()

    statuses = _refresh_tool_status()
    missing_required = [tool for tool in REQUIRED_TOOLS if not statuses.get(tool, {}).get("present")]
    if missing_required:
        for tool in missing_required:
            print(json.dumps({"type": "tool_missing", "tool": tool}), flush=True)
            print(
                f"Required tool missing: {tool}. Please install or configure a portable path.",
                file=sys.stderr,
            )
        sys.exit(3)

    if not statuses.get("smartctl", {}).get("present"):
        LOGGER.warning("smartctl not found. SMART data will be skipped.")

    total, used, free = shutil.disk_usage(mount)
    conn = init_db(db_path)
    conn.execute(
        """
        INSERT INTO drives(label, mount_path, total_bytes, free_bytes, smart_scan, scanned_at)
        VALUES(?,?,?,?,?,datetime('now'))
        ON CONFLICT(label) DO UPDATE SET
            mount_path=excluded.mount_path,
            total_bytes=excluded.total_bytes,
            free_bytes=excluded.free_bytes,
            smart_scan=excluded.smart_scan,
            scanned_at=excluded.scanned_at
        """,
        (
            label,
            str(mount.resolve()),
            int(total),
            int(free),
            try_smart_overview(TOOL_PATHS.get("smartctl")),
        ),
    )
    conn.commit()

    is_windows = os.name == "nt"
    robust_raw: Dict[str, object] = {}
    if isinstance(effective_settings, dict):
        maybe_robust = effective_settings.get("robust")
        if isinstance(maybe_robust, dict):
            robust_raw = maybe_robust
    robust_overrides = robust_overrides or {}
    robust_cfg = merge_settings(robust_raw, robust_overrides)
    robust_cfg.batch_seconds = clamp_batch_seconds(robust_cfg.batch_seconds, perf_config.profile)
    LOGGER.info("Robust settings: %s", robust_cfg.as_log_line())

    ignore_patterns: List[str] = []
    ignore_patterns.extend(list(robust_cfg.ignore))
    ignore_patterns.extend(list(robust_cfg.skip_globs))
    if ignore_patterns:
        seen_patterns: set[str] = set()
        ordered_patterns: List[str] = []
        for pattern in ignore_patterns:
            pattern = pattern.strip()
            if not pattern or pattern in seen_patterns:
                continue
            ordered_patterns.append(pattern)
            seen_patterns.add(pattern)
        ignore_patterns = ordered_patterns
    else:
        ignore_patterns = []

    state_store = ScanStateStore(conn, label, interval_seconds=int(checkpoint_seconds))
    if not resume:
        state_store.clear()
        resume_state: Dict[str, str] = {}
    else:
        resume_state = state_store.load()

    resume_path = resume_state.get("last_path_processed") if resume_state else None
    resume_key = key_for_path(resume_path, casefold=is_windows) if resume_path else None
    resume_consumed = not bool(resume_key)

    cancel_token = CancellationToken()

    metrics = {
        "dirs_scanned": 0,
        "files_seen": 0,
        "av_total": 0,
        "skipped_perm": 0,
        "skipped_toolong": 0,
        "skipped_ignored": 0,
        "retries": 0,
    }

    processed_files = 0
    processed_av = 0
    unchanged_count = 0
    total_enqueued = 0
    pending_tasks = 0
    last_processed_path = resume_path

    progress_last_emit = 0.0

    def _emit_progress(phase: str, *, force: bool = False) -> None:
        nonlocal progress_last_emit, last_processed_path
        now = time.monotonic()
        if not force and (now - progress_last_emit) < 5:
            return
        elapsed = int(now - start_time)
        payload = {
            "type": "progress",
            "phase": phase,
            "elapsed_s": elapsed,
            "dirs_scanned": metrics["dirs_scanned"],
            "files_total": metrics["files_seen"],
            "files_seen": processed_files,
            "av_seen": processed_av,
            "skipped_perm": metrics["skipped_perm"],
            "skipped_toolong": metrics["skipped_toolong"],
            "skipped_ignored": metrics["skipped_ignored"],
        }
        if metrics["av_total"]:
            payload["total_av"] = metrics["av_total"]
        try:
            print(json.dumps(payload), flush=True)
        except Exception:
            pass
        LOGGER.debug(
            "%s — dirs=%s files=%s processed=%s skipped=(perm=%s,long=%s,ignored=%s)",
            phase,
            metrics["dirs_scanned"],
            metrics["files_seen"],
            processed_files,
            metrics["skipped_perm"],
            metrics["skipped_toolong"],
            metrics["skipped_ignored"],
        )
        if resume:
            state_store.checkpoint(phase, last_processed_path if last_processed_path else None, force=force)
        progress_last_emit = now

    def _stat_path(path: str, *, follow_symlinks: bool) -> Optional[os.stat_result]:
        attempts = 0
        delay = 0.5
        while attempts < 3 and not cancel_token.is_set():
            attempts += 1
            try:
                start = time.monotonic()
                result = os.stat(path, follow_symlinks=follow_symlinks)
                elapsed = time.monotonic() - start
                if elapsed > robust_cfg.op_timeout_s:
                    raise TimeoutError(f"stat timeout after {elapsed:.1f}s")
                return result
            except PermissionError:
                metrics["skipped_perm"] += 1
                LOGGER.warning("Permission denied while stating %s", from_fs_path(path))
                return None
            except OSError as exc:
                if is_transient(exc) and attempts < 3:
                    metrics["retries"] += 1
                    time.sleep(min(2.0, delay))
                    delay *= 2
                    continue
                LOGGER.warning("stat failed for %s: %s", from_fs_path(path), exc)
                return None
        return None

    def _open_scandir(fs_path: str, display_path: str) -> Optional[os.ScandirIterator]:
        attempts = 0
        delay = 0.5
        while attempts < 3 and not cancel_token.is_set():
            attempts += 1
            try:
                start = time.monotonic()
                iterator = os.scandir(fs_path)
                elapsed = time.monotonic() - start
                if elapsed > robust_cfg.op_timeout_s:
                    iterator.close()
                    raise TimeoutError(f"scandir timeout after {elapsed:.1f}s")
                return iterator
            except PermissionError:
                metrics["skipped_perm"] += 1
                LOGGER.warning("Permission denied while enumerating %s", display_path)
                return None
            except OSError as exc:
                if is_transient(exc) and attempts < 3:
                    metrics["retries"] += 1
                    time.sleep(min(2.0, delay))
                    delay *= 2
                    continue
                LOGGER.warning("Failed to enumerate %s: %s", display_path, exc)
                return None
        LOGGER.warning("Giving up on %s after repeated failures", display_path)
        return None

    enumeration_sleep = enumerate_sleep_range(perf_config.profile, perf_config.gentle_io)

    base_sleep_range = None
    if perf_config.profile == "NETWORK":
        base_sleep_range = (0.002, 0.005)
    elif perf_config.gentle_io and perf_config.profile == "USB":
        base_sleep_range = (0.0015, 0.003)

    rate_controller = RateController(
        enabled=bool(perf_config.gentle_io or perf_config.profile == "NETWORK"),
        worker_threads=perf_config.worker_threads,
        base_sleep_range=base_sleep_range,
        latency_threshold=0.05 if perf_config.profile == "NETWORK" else 0.04,
    )
    ffmpeg_semaphore = threading.Semaphore(max(1, perf_config.ffmpeg_parallel))
    task_queue: "queue.Queue[object]" = queue.Queue(maxsize=max(1, int(robust_cfg.queue_max)))
    result_queue: "queue.Queue[WorkerResult]" = queue.Queue()
    sentinel = object()
    mediainfo_path = TOOL_PATHS.get("mediainfo")
    ffmpeg_path = TOOL_PATHS.get("ffmpeg")
    retry_delays = (0.1, 0.3, 0.9)

    existing_rows = _load_existing(conn, label, casefold=is_windows)
    if resume_key and resume_key not in existing_rows:
        resume_consumed = True

    restore_batch: List[str] = []
    pending_updates: List[Tuple[int, Optional[str], Optional[str], Optional[int], str, int]] = []
    pending_inserts: List[Tuple[str, str, int, Optional[str], Optional[str], Optional[int], str]] = []
    last_flush = time.monotonic()
    pragma_batches = 0

    def _flush_db(force: bool = False) -> None:
        nonlocal pending_updates, pending_inserts, restore_batch, last_flush, pragma_batches
        if not force:
            if (
                (len(pending_updates) + len(pending_inserts)) < robust_cfg.batch_files
                and (time.monotonic() - last_flush) < robust_cfg.batch_seconds
            ):
                return
        cur = conn.cursor()
        executed = False
        if pending_updates:
            cur.executemany(
                """
                UPDATE files
                SET size_bytes=?, hash_blake3=?, media_json=?, integrity_ok=?, mtime_utc=?, deleted=0, deleted_ts=NULL
                WHERE id=?
                """,
                pending_updates,
            )
            pending_updates = []
            executed = True
        if pending_inserts:
            cur.executemany(
                """
                INSERT INTO files(
                    drive_label, path, size_bytes, hash_blake3, media_json, integrity_ok, mtime_utc, deleted, deleted_ts
                )
                VALUES(?,?,?,?,?,?,?,0,NULL)
                """,
                pending_inserts,
            )
            pending_inserts = []
            executed = True
        if restore_batch:
            cur.executemany(
                "UPDATE files SET deleted=0, deleted_ts=NULL WHERE drive_label=? AND path=?",
                [(label, path) for path in restore_batch],
            )
            restore_batch = []
            executed = True
        if executed:
            conn.commit()
            pragma_batches += 1
            if pragma_batches % 25 == 0:
                try:
                    conn.execute("PRAGMA optimize")
                except sqlite3.Error:
                    pass
        if executed or force:
            last_flush = time.monotonic()

    def _drain_results(block: bool = False) -> None:
        nonlocal pending_tasks, processed_files, processed_av, last_processed_path
        timeout = 0.5 if block else 0.0
        while pending_tasks:
            try:
                result = result_queue.get(timeout=timeout)
            except queue.Empty:
                break
            pending_tasks -= 1
            info = result.info
            processed_files += 1
            if info.is_av:
                processed_av += 1
            last_processed_path = info.path
            if result.error_message:
                LOGGER.debug("Recorded warning for %s: %s", info.path, result.error_message)
            if info.existing_id:
                pending_updates.append(
                    (
                        int(info.size_bytes),
                        result.hash_value,
                        result.media_blob,
                        result.integrity_ok,
                        info.mtime_utc,
                        int(info.existing_id),
                    )
                )
            else:
                pending_inserts.append(
                    (
                        label,
                        info.path,
                        int(info.size_bytes),
                        result.hash_value,
                        result.media_blob,
                        result.integrity_ok,
                        info.mtime_utc,
                    )
                )
            _flush_db(force=False)
            _emit_progress("hashing")

    def _process_file(info: FileInfo) -> WorkerResult:
        integrity_ok: Optional[int] = None if info.is_av else 1
        media_blob: Optional[str] = None
        hash_value: Optional[str] = None
        error_message: Optional[str] = None
        attempts = 0

        while not cancel_token.is_set():
            try:
                delay = rate_controller.before_task(task_queue.qsize())
                if delay > 0:
                    time.sleep(delay)

                def _on_chunk(bytes_read: int, elapsed: float) -> None:
                    if bytes_read > 0:
                        rate_controller.note_io(elapsed)

                hash_value = hash_blake3(
                    info.fs_path,
                    chunk=perf_config.hash_chunk_bytes,
                    on_chunk=_on_chunk,
                )
                metadata = mediainfo_json(info.fs_path, mediainfo_path) if info.is_av else None
                if metadata is not None:
                    media_blob = json.dumps(metadata, ensure_ascii=False)
                    integrity_ok = 0 if metadata.get("error") else 1
                else:
                    media_blob = None
                    integrity_ok = None if info.is_av else 1
                if info.is_av and not cancel_token.is_set():
                    with ffmpeg_semaphore:
                        ok = ffmpeg_verify(info.fs_path, ffmpeg_path)
                    if ok is False:
                        integrity_ok = 0
                rate_controller.note_success()
                error_message = None
                break
            except (OSError, IOError) as exc:
                rate_controller.note_error()
                attempts += 1
                if attempts < len(retry_delays):
                    time.sleep(retry_delays[attempts - 1])
                    continue
                LOGGER.warning("I/O error while processing %s: %s", info.path, exc)
                media_blob = json.dumps({"error": str(exc)}, ensure_ascii=False)
                integrity_ok = 0
                hash_value = None
                error_message = str(exc)
                break
            except Exception as exc:
                rate_controller.note_error()
                LOGGER.exception("Failed to process %s", info.path)
                media_blob = json.dumps({"error": str(exc)}, ensure_ascii=False)
                integrity_ok = 0
                hash_value = None
                error_message = str(exc)
                break
        else:
            error_message = "cancelled"

        return WorkerResult(
            info=info,
            hash_value=hash_value,
            media_blob=media_blob,
            integrity_ok=integrity_ok,
            error_message=error_message,
        )

    def _worker() -> None:
        while True:
            try:
                item = task_queue.get(timeout=0.5)
            except queue.Empty:
                if cancel_token.is_set():
                    continue
                continue
            try:
                if item is sentinel:
                    break
                assert isinstance(item, FileInfo)
                if cancel_token.is_set():
                    continue
                result_queue.put(_process_file(item))
            finally:
                task_queue.task_done()

    workers: List[threading.Thread] = []
    for _ in range(perf_config.worker_threads):
        thread = threading.Thread(target=_worker, name="scan-worker")
        thread.daemon = True
        thread.start()
        workers.append(thread)

    try:
        root_display = str(mount)
        try:
            root_fs = to_fs_path(root_display, mode=robust_cfg.long_paths)
        except PathTooLongError:
            metrics["skipped_toolong"] += 1
            LOGGER.error("Mount path too long: %s", root_display)
            cancel_token.set()
            root_fs = root_display

        stack: Deque[Tuple[str, str]] = deque()
        stack.append((root_display, root_fs))
        visited_dirs: set[Tuple[int, int]] = set()
        if robust_cfg.follow_symlinks:
            root_stat = _stat_path(root_fs, follow_symlinks=True)
            if root_stat:
                visited_dirs.add((root_stat.st_dev, root_stat.st_ino))

        while stack and not cancel_token.is_set():
            display_dir, fs_dir = stack.pop()
            iterator = _open_scandir(fs_dir, display_dir)
            if iterator is None:
                continue
            metrics["dirs_scanned"] += 1
            with iterator as it:
                for entry in it:
                    if cancel_token.is_set():
                        break
                    entry_fs = entry.path
                    display_entry = from_fs_path(entry_fs)
                    if robust_cfg.skip_hidden and is_hidden(entry, fs_path=entry_fs, display_path=display_entry):
                        metrics["skipped_ignored"] += 1
                        continue
                    if ignore_patterns and should_ignore(display_entry, patterns=ignore_patterns):
                        metrics["skipped_ignored"] += 1
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=robust_cfg.follow_symlinks):
                            try:
                                next_fs = to_fs_path(display_entry, mode=robust_cfg.long_paths)
                            except PathTooLongError:
                                metrics["skipped_toolong"] += 1
                                LOGGER.warning("Skipping long directory %s", display_entry)
                                continue
                            if robust_cfg.follow_symlinks:
                                dir_stat = _stat_path(next_fs, follow_symlinks=True)
                                if not dir_stat:
                                    continue
                                inode_key = (dir_stat.st_dev, dir_stat.st_ino)
                                if inode_key in visited_dirs:
                                    LOGGER.warning("Detected symlink loop at %s", display_entry)
                                    continue
                                visited_dirs.add(inode_key)
                            stack.append((display_entry, next_fs))
                            continue
                    except OSError:
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    stat_result = _stat_path(entry_fs, follow_symlinks=False)
                    if stat_result is None:
                        continue
                    try:
                        fs_file = to_fs_path(display_entry, mode=robust_cfg.long_paths)
                    except PathTooLongError:
                        metrics["skipped_toolong"] += 1
                        LOGGER.warning("Skipping long path %s", display_entry)
                        continue
                    info = FileInfo(
                        path=display_entry,
                        fs_path=fs_file,
                        size_bytes=int(stat_result.st_size),
                        mtime_utc=_iso_from_timestamp(stat_result.st_mtime),
                        is_av=_is_av(display_entry),
                    )
                    metrics["files_seen"] += 1
                    if info.is_av:
                        metrics["av_total"] += 1
                    existing_key = key_for_path(info.path, casefold=is_windows)
                    existing_row = existing_rows.pop(existing_key, None)
                    if existing_row is not None:
                        info.existing_id = existing_row["id"]
                        info.was_deleted = bool(existing_row["deleted"])
                        if (
                            not full_rescan
                            and not info.was_deleted
                            and int(existing_row["size_bytes"]) == info.size_bytes
                            and existing_row["mtime_utc"] == info.mtime_utc
                        ):
                            restore_batch.append(existing_row["path"])
                            unchanged_count += 1
                            if len(restore_batch) >= 2000:
                                _flush_db(force=True)
                            continue
                    if resume and not resume_consumed:
                        processed_files += 1
                        if info.is_av:
                            processed_av += 1
                        last_processed_path = info.path
                        if resume_key == existing_key:
                            resume_consumed = True
                        continue
                    while not cancel_token.is_set():
                        try:
                            task_queue.put(info, timeout=0.5)
                            pending_tasks += 1
                            total_enqueued += 1
                            break
                        except queue.Full:
                            _drain_results(block=True)
                            _flush_db(force=False)
                    if cancel_token.is_set():
                        break
                    if enumeration_sleep:
                        time.sleep(random.uniform(*enumeration_sleep))
                    if debug_slow:
                        time.sleep(0.01)
                    _drain_results(block=False)
                    _emit_progress("enumerating")
    except KeyboardInterrupt:
        cancel_token.set()
        LOGGER.warning("Scan cancelled by user.")
    except Exception as exc:
        cancel_token.set()
        LOGGER.exception("Enumeration failure: %s", exc)

    _emit_progress("enumerating", force=True)

    for _ in workers:
        while True:
            try:
                task_queue.put(sentinel, timeout=0.5)
                break
            except queue.Full:
                _drain_results(block=True)
                _flush_db(force=False)

    while pending_tasks:
        _drain_results(block=True)
    _flush_db(force=True)

    task_queue.join()
    for thread in workers:
        thread.join()

    _emit_progress("hashing", force=True)

    deleted_count = 0
    deleted_examples: List[str] = []
    if not cancel_token.is_set():
        stale_paths = [row["path"] for row in existing_rows.values()]
        deleted_count, deleted_examples = _mark_deleted(conn, label, deleted_paths=stale_paths)
        if deleted_count:
            LOGGER.info(
                "Marked %s files as deleted (examples: %s)",
                deleted_count,
                ", ".join(deleted_examples) if deleted_examples else "—",
            )

    if resume:
        state_store.checkpoint("hashing", last_processed_path, force=True)
        state_store.checkpoint("finalizing", None, force=True)
        state_store.clear()

    duration = time.perf_counter() - start_time
    LOGGER.info(
        "Scan complete for %s. Processed %s files (%s AV) in %.2fs.",
        label,
        processed_files,
        processed_av,
        duration,
    )
    LOGGER.info(
        "Skipped: perm=%s, long=%s, ignored=%s (retries=%s)",
        metrics["skipped_perm"],
        metrics["skipped_toolong"],
        metrics["skipped_ignored"],
        metrics["retries"],
    )
    conn.close()
    return {
        "total_files": metrics["files_seen"],
        "av_files": metrics["av_total"],
        "pending": total_enqueued,
        "unchanged": unchanged_count,
        "deleted": deleted_count,
        "duration_seconds": duration,
        "performance": perf_config.as_dict(),
        "skipped_perm": metrics["skipped_perm"],
        "skipped_toolong": metrics["skipped_toolong"],
        "skipped_ignored": metrics["skipped_ignored"],
    }



def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan a drive and populate the VideoCatalog databases."
    )
    parser.add_argument("--label", help="Drive label to record in the catalog.")
    parser.add_argument(
        "--mount",
        dest="mount_path",
        help="Filesystem path where the drive is mounted.",
    )
    parser.add_argument(
        "--catalog-db",
        dest="catalog_db",
        help="Optional path to the catalog database (defaults to working dir).",
    )
    parser.add_argument(
        "--shard-db",
        dest="shard_db",
        help="Optional path to the shard database (defaults to working dir).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging output.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug helpers such as slow enumeration sleeps.",
    )
    parser.add_argument(
        "--debug-slow-enumeration",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--batch-files",
        type=int,
        help="Batch database commits after this many files (default: 1000).",
    )
    parser.add_argument(
        "--batch-seconds",
        type=float,
        help="Maximum seconds between batched commits (default: 2).",
    )
    parser.add_argument(
        "--queue-max",
        type=int,
        help="Maximum work items queued ahead of hashing (default: 10000).",
    )
    parser.add_argument(
        "--skip-hidden",
        action="store_true",
        help="Skip hidden/system files and directories during enumeration.",
    )
    parser.add_argument(
        "--skip-glob",
        action="append",
        metavar="PATTERN",
        help="Glob pattern to ignore (repeatable).",
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinks and junctions; cycles are detected and skipped.",
    )
    parser.add_argument(
        "--long-paths",
        choices=["auto", "force", "off"],
        help="Control Windows extended-length path handling (default: auto).",
    )
    parser.add_argument(
        "--op-timeout",
        type=int,
        help="Filesystem operation timeout in seconds before retry (default: 30).",
    )
    parser.add_argument(
        "--full-rescan",
        action="store_true",
        help="Force a full rescan of all files, ignoring the delta optimizer.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start a new scan even if a checkpoint is present.",
    )
    parser.add_argument(
        "--checkpoint-seconds",
        type=int,
        default=5,
        help="Seconds between resume checkpoints (default: 5).",
    )
    parser.add_argument(
        "--perf-profile",
        choices=["AUTO", "SSD", "HDD", "USB", "NETWORK"],
        help="Override automatic performance profile detection.",
    )
    parser.add_argument(
        "--perf-threads",
        type=int,
        help="Override worker thread count.",
    )
    parser.add_argument(
        "--perf-chunk",
        type=int,
        help="Override hash chunk size in bytes.",
    )
    parser.add_argument(
        "--perf-ffmpeg",
        type=int,
        help="Override FFmpeg parallelism.",
    )
    parser.add_argument(
        "--perf-gentle-io",
        dest="perf_gentle_io",
        action="store_const",
        const=True,
        help="Force-enable gentle I/O throttling.",
    )
    parser.add_argument(
        "--no-perf-gentle-io",
        dest="perf_gentle_io",
        action="store_const",
        const=False,
        help="Disable gentle I/O throttling even if recommended.",
    )
    parser.set_defaults(perf_gentle_io=None)
    parser.add_argument(
        "--export-csv",
        nargs="?",
        const="",
        help="Export results to CSV after scanning. Optional path overrides the default.",
    )
    parser.add_argument(
        "--export-jsonl",
        nargs="?",
        const="",
        help="Export results to JSONL after scanning. Optional path overrides the default.",
    )
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="Include rows marked as deleted in exports.",
    )
    parser.add_argument(
        "--av-only",
        action="store_true",
        help="Only include audio/video files in exports.",
    )
    parser.add_argument(
        "--since",
        dest="since",
        help="Only include files updated at or after the given ISO 8601 UTC timestamp.",
    )
    parser.add_argument(
        "positional",
        nargs="*",
        help=argparse.SUPPRESS,
    )

    namespace = parser.parse_args(argv)
    positional = list(namespace.positional)

    if namespace.label is None and positional:
        namespace.label = positional.pop(0)
    if namespace.mount_path is None and positional:
        namespace.mount_path = positional.pop(0)
    if namespace.catalog_db is None and positional:
        namespace.catalog_db = positional.pop(0)

    if namespace.label is None or namespace.mount_path is None:
        parser.error("Both --label and --mount are required.")

    namespace.positional = positional
    return namespace


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        LOGGER.error("Failed to create directory %s: %s", path, exc)
        sys.exit(4)


def _format_duration(seconds: float) -> str:
    total_seconds = int(round(max(0.0, float(seconds))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _format_filter_summary(filters: ExportFilters) -> str:
    since = filters.since_utc or "—"
    include = "true" if filters.include_deleted else "false"
    av_only = "true" if filters.av_only else "false"
    return f"include_deleted={include}, av_only={av_only}, since={since}"


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    catalog_db_path = (
        _expand_user_path(args.catalog_db)
        if args.catalog_db
        else Path(DEFAULT_DB_PATH)
    )
    shard_db_path = (
        _expand_user_path(args.shard_db)
        if args.shard_db
        else get_shard_db_path(WORKING_DIR_PATH, args.label)
    )

    _ensure_directory(catalog_db_path.parent)
    _ensure_directory(shard_db_path.parent)

    startup_info = (
        f"Working directory: {WORKING_DIR_PATH} | "
        f"catalog: {catalog_db_path} | shard: {shard_db_path}"
    )
    LOGGER.info(startup_info)

    try:
        since_value = parse_since(getattr(args, "since", None))
    except ValueError as exc:
        LOGGER.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 2

    filters = ExportFilters(
        include_deleted=bool(getattr(args, "include_deleted", False)),
        av_only=bool(getattr(args, "av_only", False)),
        since_utc=since_value,
    )

    debug_flag = bool(args.debug or args.debug_slow_enumeration)
    checkpoint_seconds = max(1, int(getattr(args, "checkpoint_seconds", 5)))
    perf_cli_overrides: Dict[str, object] = {}
    if getattr(args, "perf_profile", None) is not None:
        perf_cli_overrides["profile"] = args.perf_profile
    if getattr(args, "perf_threads", None) is not None:
        perf_cli_overrides["worker_threads"] = args.perf_threads
    if getattr(args, "perf_chunk", None) is not None:
        perf_cli_overrides["hash_chunk_bytes"] = args.perf_chunk
    if getattr(args, "perf_ffmpeg", None) is not None:
        perf_cli_overrides["ffmpeg_parallel"] = args.perf_ffmpeg
    if getattr(args, "perf_gentle_io", None) is not None:
        perf_cli_overrides["gentle_io"] = args.perf_gentle_io

    robust_cli_overrides: Dict[str, object] = {}
    if getattr(args, "batch_files", None) is not None:
        robust_cli_overrides["batch_files"] = args.batch_files
    if getattr(args, "batch_seconds", None) is not None:
        robust_cli_overrides["batch_seconds"] = args.batch_seconds
    if getattr(args, "queue_max", None) is not None:
        robust_cli_overrides["queue_max"] = args.queue_max
    if getattr(args, "skip_hidden", False):
        robust_cli_overrides["skip_hidden"] = True
    if getattr(args, "skip_glob", None):
        robust_cli_overrides["skip_globs"] = args.skip_glob
    if getattr(args, "follow_symlinks", False):
        robust_cli_overrides["follow_symlinks"] = True
    if getattr(args, "long_paths", None) is not None:
        robust_cli_overrides["long_paths"] = args.long_paths
    if getattr(args, "op_timeout", None) is not None:
        robust_cli_overrides["op_timeout_s"] = args.op_timeout

    settings_data = load_settings(WORKING_DIR_PATH)

    result = scan_drive(
        args.label,
        args.mount_path,
        str(catalog_db_path),
        full_rescan=bool(getattr(args, "full_rescan", False)),
        resume=not bool(getattr(args, "no_resume", False)),
        checkpoint_seconds=checkpoint_seconds,
        debug_slow=debug_flag,
        settings=settings_data,
        perf_overrides=perf_cli_overrides,
        robust_overrides=robust_cli_overrides,
    )

    total_files = int(result.get("total_files", 0)) if isinstance(result, dict) else 0
    av_files = int(result.get("av_files", 0)) if isinstance(result, dict) else 0
    duration_seconds = float(result.get("duration_seconds", 0.0)) if isinstance(result, dict) else 0.0
    summary_line = (
        "Done — total files: "
        f"{total_files:,} — AV files: {av_files:,} — duration: {_format_duration(duration_seconds)}"
    )

    export_requests: list[tuple[str, Optional[str]]] = []
    if getattr(args, "export_csv", None) is not None:
        export_requests.append(("csv", args.export_csv))
    if getattr(args, "export_jsonl", None) is not None:
        export_requests.append(("jsonl", args.export_jsonl))

    for fmt, override in export_requests:
        override_path = None if not override else _expand_user_path(override)
        target_display = str(override_path) if override_path else "default"
        LOGGER.info(
            "%s export starting → %s (%s)",
            fmt.upper(),
            target_display,
            _format_filter_summary(filters),
        )
        try:
            export_result = export_catalog(
                catalog_db_path,
                WORKING_DIR_PATH,
                args.label,
                filters,
                fmt=fmt,
                output_path=override_path,
            )
        except Exception as exc:
            LOGGER.error("%s export failed: %s", fmt.upper(), exc)
            print(f"[ERROR] {fmt.upper()} export failed: {exc}", file=sys.stderr)
            print(summary_line)
            return 5
        LOGGER.info(
            "%s export finished: %s (%s rows)",
            fmt.upper(),
            export_result.path,
            export_result.rows,
        )

    print(summary_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
