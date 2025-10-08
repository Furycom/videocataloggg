import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import hashlib
import importlib

from paths import (
    ensure_working_dir_structure,
    get_catalog_db_path,
    get_shard_db_path,
    resolve_working_dir,
)

WORKING_DIR_PATH = resolve_working_dir()
ensure_working_dir_structure(WORKING_DIR_PATH)
DEFAULT_DB_PATH = str(get_catalog_db_path(WORKING_DIR_PATH))
LOGGER = logging.getLogger("videocatalog.scan")


def _expand_user_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded)


_blake3_spec = importlib.util.find_spec("blake3")
_blake3_hash = importlib.import_module("blake3").blake3 if _blake3_spec is not None else None


def _has_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None


_VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".m4v",
    ".ts",
    ".m2ts",
    ".webm",
    ".mpg",
    ".mpeg",
    ".flv",
    ".vob",
    ".ogv",
    ".3gp",
}
_AUDIO_EXTS = {
    ".mp3",
    ".flac",
    ".aac",
    ".m4a",
    ".wav",
    ".wma",
    ".ogg",
    ".opus",
    ".alac",
    ".aiff",
    ".ape",
    ".dsf",
    ".dff",
}
_AV_EXTS = _VIDEO_EXTS | _AUDIO_EXTS


@dataclass
class FileInfo:
    path: str
    size_bytes: int
    mtime: float
    mtime_iso: str
    is_av: bool


@dataclass
class WorkItem:
    info: FileInfo
    op: str  # "insert" or "update"


class ProgressEmitter:
    def __init__(self) -> None:
        self._start: Optional[float] = None
        self._last_emit: float = 0.0

    def emit(
        self,
        phase: str,
        files_seen: int,
        av_seen: int,
        *,
        total_all: Optional[int] = None,
        total_av: Optional[int] = None,
        force: bool = False,
    ) -> None:
        now = time.perf_counter()
        if self._start is None:
            self._start = now
        if not force and (now - self._last_emit) < 5.0:
            return
        elapsed = int(now - (self._start or now))
        payload: Dict[str, object] = {
            "type": "progress",
            "phase": phase,
            "elapsed_s": elapsed,
            "files_seen": files_seen,
            "done_all": files_seen,
            "av_seen": av_seen,
            "done_av": av_seen,
        }
        if total_all is not None:
            payload["files_total"] = total_all
            payload["total_all"] = total_all
        if total_av is not None:
            payload["total_av"] = total_av
        print(json.dumps(payload), flush=True)
        self._last_emit = now


class ScanStateStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS scan_state("
            "key TEXT PRIMARY KEY,"
            "value TEXT"
            ")"
        )
        self.conn.commit()

    def load(self) -> Dict[str, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT key, value FROM scan_state")
        return {row[0]: row[1] for row in cur.fetchall()}

    def save(self, phase: str, last_path: Optional[str]) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        rows = [
            ("phase", phase),
            ("last_path_processed", last_path or ""),
            ("timestamp", now),
        ]
        cur = self.conn.cursor()
        cur.executemany(
            "REPLACE INTO scan_state(key, value) VALUES(?, ?)",
            rows,
        )
        self.conn.commit()

    def clear(self) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM scan_state")
        self.conn.commit()


class CheckpointManager:
    def __init__(self, store: Optional[ScanStateStore], seconds: int) -> None:
        self.store = store
        self.seconds = max(1, int(seconds))
        self._last = time.monotonic()

    def maybe(self, phase: str, last_path: Optional[str]) -> None:
        if not self.store:
            return
        now = time.monotonic()
        if (now - self._last) >= self.seconds:
            self.store.save(phase, last_path)
            self._last = now

    def force(self, phase: str, last_path: Optional[str] = "") -> None:
        if not self.store:
            return
        self.store.save(phase, last_path)
        self._last = time.monotonic()


def _hash_file(file_path: str) -> Optional[str]:
    try:
        if _blake3_hash is not None:
            hasher = _blake3_hash()
        else:
            hasher = hashlib.sha256()
        with open(file_path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None


def _mediainfo(file_path: str) -> Optional[dict]:
    try:
        result = subprocess.check_output(
            ["mediainfo", "--Output=JSON", file_path],
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.strip():
            return json.loads(result)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 127:
            return None
        return {"error": exc.output.strip() if exc.output else "mediainfo_failed"}
    except FileNotFoundError:
        return None
    except Exception as exc:
        return {"error": str(exc)}
    return None


def _ffmpeg_verify(file_path: str) -> Optional[bool]:
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-i",
                file_path,
                "-f",
                "null",
                "-",
                "-nostdin",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode == 0:
            return True
        return False
    except FileNotFoundError:
        return None
    except Exception:
        return False


def _try_smart_overview() -> Optional[str]:
    if not _has_cmd("smartctl"):
        return None
    acc = {"scan": None, "details": []}
    try:
        proc = subprocess.run(["smartctl", "--scan-open"], capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout:
            acc["scan"] = proc.stdout
        for idx in range(0, 10):
            detail = subprocess.run(
                ["smartctl", "-i", "-H", "-A", "-j", fr"\\.\PhysicalDrive{idx}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if detail.returncode == 0 and detail.stdout:
                try:
                    acc["details"].append(json.loads(detail.stdout))
                except json.JSONDecodeError:
                    pass
        return json.dumps(acc, ensure_ascii=False)
    except Exception:
        return None


def _ensure_catalog_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drives(
            id INTEGER PRIMARY KEY,
            label TEXT NOT NULL UNIQUE,
            mount_path TEXT NOT NULL,
            total_bytes INTEGER,
            free_bytes INTEGER,
            smart_scan TEXT,
            scanned_at TEXT NOT NULL,
            scan_mode TEXT
        )
        """
    )
    conn.commit()


def _ensure_shard_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            size_bytes INTEGER,
            is_av INTEGER DEFAULT 0,
            hash_blake3 TEXT,
            media_json TEXT,
            integrity_ok INTEGER,
            mtime_utc TEXT,
            deleted INTEGER DEFAULT 0,
            deleted_ts TEXT
        )
        """
    )
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(files)")
    existing_columns = {row[1] for row in cur.fetchall()}
    if "is_av" not in existing_columns:
        cur.execute("ALTER TABLE files ADD COLUMN is_av INTEGER DEFAULT 0")
    if "hash_blake3" not in existing_columns:
        cur.execute("ALTER TABLE files ADD COLUMN hash_blake3 TEXT")
    if "media_json" not in existing_columns:
        cur.execute("ALTER TABLE files ADD COLUMN media_json TEXT")
    if "integrity_ok" not in existing_columns:
        cur.execute("ALTER TABLE files ADD COLUMN integrity_ok INTEGER")
    if "mtime_utc" not in existing_columns:
        cur.execute("ALTER TABLE files ADD COLUMN mtime_utc TEXT")
    if "deleted" not in existing_columns:
        cur.execute("ALTER TABLE files ADD COLUMN deleted INTEGER DEFAULT 0")
    if "deleted_ts" not in existing_columns:
        cur.execute("ALTER TABLE files ADD COLUMN deleted_ts TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_files_path ON files(path)")
    conn.commit()


def _init_catalog_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    _ensure_catalog_schema(conn)
    return conn


def _init_shard_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    _ensure_shard_schema(conn)
    return conn


class DriveScanner:
    def __init__(
        self,
        *,
        label: str,
        mount_path: str,
        catalog_db: Path,
        shard_db: Path,
        full_rescan: bool,
        resume: bool,
        checkpoint_seconds: int,
        debug_slow: bool = False,
    ) -> None:
        self.label = label
        self.mount_path = Path(mount_path)
        self.catalog_db = catalog_db
        self.shard_db = shard_db
        self.full_rescan = full_rescan
        self.resume = resume
        self.checkpoint_seconds = max(1, int(checkpoint_seconds))
        self.debug_slow = debug_slow
        self.emitter = ProgressEmitter()
        self._state_store: Optional[ScanStateStore] = None
        self._checkpoints = CheckpointManager(None, self.checkpoint_seconds)

    # ----- enumeration helpers -----
    def _enumerate_files(self) -> List[FileInfo]:
        stack: List[Path] = [self.mount_path]
        results: List[FileInfo] = []
        total_av = 0
        last_emit = time.perf_counter()
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    for entry in iterator:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        try:
                            stat = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        suffix = Path(entry.name).suffix.lower()
                        is_av = suffix in _AV_EXTS
                        mtime_iso = datetime.utcfromtimestamp(stat.st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
                        info = FileInfo(
                            path=entry.path,
                            size_bytes=int(stat.st_size),
                            mtime=stat.st_mtime,
                            mtime_iso=mtime_iso,
                            is_av=is_av,
                        )
                        results.append(info)
                        if is_av:
                            total_av += 1
                        now = time.perf_counter()
                        if (now - last_emit) >= 5.0:
                            self.emitter.emit("enumerating", len(results), total_av)
                            self._checkpoints.maybe("enumerating", entry.path)
                            last_emit = now
                        if self.debug_slow:
                            time.sleep(0.01)
            except PermissionError:
                LOGGER.warning("Permission denied while scanning %s", current)
            except FileNotFoundError:
                continue
        results.sort(key=lambda item: item.path)
        self.emitter.emit("enumerating", len(results), total_av, force=True)
        self._checkpoints.force("enumerating", results[-1].path if results else "")
        return results

    # ----- db helpers -----
    def _load_existing(self, cur: sqlite3.Cursor) -> Dict[str, Dict[str, object]]:
        cur.execute(
            "SELECT path, size_bytes, is_av, integrity_ok, mtime_utc, deleted FROM files",
        )
        data: Dict[str, Dict[str, object]] = {}
        for row in cur.fetchall():
            path, size_bytes, is_av, integrity_ok, mtime_utc, deleted = row
            data[path] = {
                "size_bytes": size_bytes,
                "is_av": int(is_av or 0),
                "integrity_ok": integrity_ok,
                "mtime_utc": mtime_utc,
                "deleted": int(deleted or 0),
            }
        return data

    def _mark_deleted(self, cur: sqlite3.Cursor, paths: Iterable[str]) -> int:
        rows = list(paths)
        if not rows:
            return 0
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        cur.executemany(
            "UPDATE files SET deleted=1, deleted_ts=?, integrity_ok=NULL WHERE path=?",
            [(now, path) for path in rows],
        )
        return len(rows)

    def _insert_rows(self, cur: sqlite3.Cursor, rows: List[Tuple]) -> None:
        if not rows:
            return
        cur.executemany(
            """
            INSERT INTO files(
                path,
                size_bytes,
                is_av,
                hash_blake3,
                media_json,
                integrity_ok,
                mtime_utc,
                deleted,
                deleted_ts
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )

    def _update_rows(self, cur: sqlite3.Cursor, rows: List[Tuple]) -> None:
        if not rows:
            return
        cur.executemany(
            """
            UPDATE files SET
                size_bytes=?,
                is_av=?,
                hash_blake3=?,
                media_json=?,
                integrity_ok=?,
                mtime_utc=?,
                deleted=0,
                deleted_ts=NULL
            WHERE path=?
            """,
            rows,
        )

    def _load_pending_ffmpeg(self, cur: sqlite3.Cursor, disk_paths: set[str]) -> List[str]:
        cur.execute(
            "SELECT path FROM files WHERE deleted=0 AND is_av=1 AND integrity_ok IS NULL",
        )
        return [row[0] for row in cur.fetchall() if row[0] in disk_paths]

    # ----- processing helpers -----
    def _process_file(self, item: WorkItem) -> Tuple[Optional[Tuple], Optional[Tuple], bool, int]:
        info = item.info
        hash_value = _hash_file(info.path)
        media = _mediainfo(info.path)
        media_json = json.dumps(media, ensure_ascii=False) if isinstance(media, dict) else None
        integrity = None
        needs_ffmpeg = False
        av_increment = 0
        if info.is_av:
            if isinstance(media, dict):
                if media.get("error"):
                    integrity = 0
                    av_increment = 1
                else:
                    needs_ffmpeg = True
                    av_increment = 1
            else:
                integrity = None
        else:
            integrity = 1
        insert_row: Optional[Tuple] = None
        update_row: Optional[Tuple] = None
        if item.op == "insert":
            insert_row = (
                info.path,
                info.size_bytes,
                1 if info.is_av else 0,
                hash_value,
                media_json,
                integrity,
                info.mtime_iso,
                0,
                None,
            )
        else:
            update_row = (
                info.size_bytes,
                1 if info.is_av else 0,
                hash_value,
                media_json,
                integrity,
                info.mtime_iso,
                info.path,
            )
        return insert_row, update_row, needs_ffmpeg, av_increment

    # ----- main entry -----
    def run(self) -> None:
        if not self.mount_path.exists():
            print(f"[ERROR] Mount path not found: {self.mount_path}")
            sys.exit(2)

        mediainfo_available = _has_cmd("mediainfo")
        ffmpeg_available = _has_cmd("ffmpeg")
        missing_tools = []
        if not mediainfo_available:
            missing_tools.append("mediainfo")
        if not ffmpeg_available:
            missing_tools.append("ffmpeg")
        if missing_tools:
            for tool in missing_tools:
                print(json.dumps({"type": "tool_missing", "tool": tool}), flush=True)
            print(
                f"[ERROR] Missing required tool(s): {', '.join(missing_tools)}",
                file=sys.stderr,
            )
            sys.exit(3)

        print(
            f"Delta rescan = {not self.full_rescan}, resume = {self.resume}, checkpoint = {self.checkpoint_seconds} s",
            flush=True,
        )

        catalog = _init_catalog_db(self.catalog_db)
        shard = _init_shard_db(self.shard_db)
        try:
            self._state_store = ScanStateStore(shard) if self.resume else None
            self._checkpoints = CheckpointManager(self._state_store, self.checkpoint_seconds)
            if not self.resume and self._state_store:
                self._state_store.clear()
            state = self._state_store.load() if self._state_store else {}

            files = self._enumerate_files()
            total_all = len(files)
            total_av = sum(1 for info in files if info.is_av)

            disk_paths = {info.path for info in files}

            shard_cur = shard.cursor()
            existing = self._load_existing(shard_cur)

            deleted_count = 0
            if not self.full_rescan:
                deleted_candidates = [path for path in existing.keys() if path not in disk_paths and not existing[path]["deleted"]]
                deleted_count = self._mark_deleted(shard_cur, deleted_candidates)
                if deleted_count:
                    LOGGER.info("Marked %s files as deleted", deleted_count)

            work_items: List[WorkItem] = []
            unchanged = 0
            done_av = 0
            for info in files:
                previous = existing.get(info.path)
                if self.full_rescan:
                    op = "insert" if previous is None else "update"
                    work_items.append(WorkItem(info=info, op=op))
                    continue
                if previous is None or previous.get("deleted"):
                    work_items.append(WorkItem(info=info, op="insert"))
                    continue
                if previous.get("size_bytes") != info.size_bytes or previous.get("mtime_utc") != info.mtime_iso:
                    work_items.append(WorkItem(info=info, op="update"))
                    continue
                unchanged += 1
                if info.is_av and previous.get("integrity_ok") in (0, 1):
                    done_av += 1

            work_items.sort(key=lambda item: item.info.path)
            processed_all = unchanged

            if state and state.get("phase") == "hashing":
                last_path = state.get("last_path_processed") or ""
                if last_path:
                    skipped = 0
                    new_items: List[WorkItem] = []
                    for item in work_items:
                        if item.info.path <= last_path:
                            skipped += 1
                            if item.info.is_av:
                                done_av += 1
                            continue
                        new_items.append(item)
                    processed_all += skipped
                    work_items = new_items
            elif state and state.get("phase") == "ffmpeg":
                processed_all = total_all
                done_av = sum(
                    1
                    for info in files
                    if info.is_av and existing.get(info.path, {}).get("integrity_ok") in (0, 1)
                )

            self.emitter.emit(
                "hashing" if work_items else "mediainfo",
                processed_all,
                done_av,
                total_all=total_all,
                total_av=total_av,
                force=True,
            )

            insert_rows: List[Tuple] = []
            update_rows: List[Tuple] = []
            ffmpeg_queue: List[str] = []
            max_workers = min(32, max(1, (os.cpu_count() or 1) * 2))
            self._checkpoints.force("hashing" if work_items else "mediainfo", "")

            def submit_batch(batch: List[WorkItem]) -> None:
                nonlocal processed_all, done_av
                if not batch:
                    return
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {executor.submit(self._process_file, item): item for item in batch}
                    for future in as_completed(future_map):
                        item = future_map[future]
                        info = item.info
                        try:
                            insert_row, update_row, needs_ffmpeg, av_inc = future.result()
                        except Exception as exc:
                            LOGGER.error("Worker error on %s: %s", info.path, exc)
                            processed_all += 1
                            if info.is_av:
                                done_av += 1
                            self.emitter.emit(
                                "hashing",
                                processed_all,
                                done_av,
                                total_all=total_all,
                                total_av=total_av,
                            )
                            continue
                        if insert_row:
                            insert_rows.append(insert_row)
                        if update_row:
                            update_rows.append(update_row)
                        if needs_ffmpeg:
                            ffmpeg_queue.append(info.path)
                        processed_all += 1
                        done_av += av_inc
                        if len(insert_rows) >= 100:
                            self._insert_rows(shard_cur, insert_rows)
                            shard.commit()
                            insert_rows.clear()
                        if len(update_rows) >= 100:
                            self._update_rows(shard_cur, update_rows)
                            shard.commit()
                            update_rows.clear()
                        self._checkpoints.maybe("hashing", info.path)
                        self.emitter.emit(
                            "hashing",
                            processed_all,
                            done_av,
                            total_all=total_all,
                            total_av=total_av,
                        )
                if insert_rows:
                    self._insert_rows(shard_cur, insert_rows)
                    insert_rows.clear()
                if update_rows:
                    self._update_rows(shard_cur, update_rows)
                    update_rows.clear()
                shard.commit()

            submit_batch(work_items)

            if not work_items:
                shard.commit()

            pending_ffmpeg = self._load_pending_ffmpeg(shard_cur, disk_paths)
            ffmpeg_candidates = sorted({*pending_ffmpeg, *ffmpeg_queue})

            if state and state.get("phase") == "ffmpeg":
                last_path = state.get("last_path_processed") or ""
                if last_path:
                    ffmpeg_candidates = [p for p in ffmpeg_candidates if p > last_path]

            if ffmpeg_candidates:
                self.emitter.emit(
                    "ffmpeg",
                    processed_all,
                    done_av,
                    total_all=total_all,
                    total_av=total_av,
                    force=True,
                )
                self._checkpoints.force("ffmpeg", "")
                updates: List[Tuple[int, str]] = []
                with ThreadPoolExecutor(max_workers=min(8, max(1, os.cpu_count() or 1))) as executor:
                    future_map = {executor.submit(_ffmpeg_verify, path): path for path in ffmpeg_candidates}
                    for future in as_completed(future_map):
                        path = future_map[future]
                        try:
                            ok = future.result()
                        except Exception:
                            ok = False
                        if ok is None:
                            continue
                        updates.append((1 if ok else 0, path))
                        if len(updates) >= 100:
                            shard_cur.executemany(
                                "UPDATE files SET integrity_ok=? WHERE path=?",
                                [(val, p) for val, p in updates],
                            )
                            shard.commit()
                            updates.clear()
                        self._checkpoints.maybe("ffmpeg", path)
                        self.emitter.emit(
                            "ffmpeg",
                            processed_all,
                            done_av,
                            total_all=total_all,
                            total_av=total_av,
                        )
                if updates:
                    shard_cur.executemany(
                        "UPDATE files SET integrity_ok=? WHERE path=?",
                        [(val, p) for val, p in updates],
                    )
                    shard.commit()
            else:
                shard.commit()

            self._checkpoints.force("finalizing", "")
            self.emitter.emit(
                "finalizing",
                total_all,
                done_av,
                total_all=total_all,
                total_av=total_av,
                force=True,
            )
            if self._state_store:
                self._state_store.clear()

            total_bytes, _, free_bytes = shutil.disk_usage(self.mount_path)
            smart_blob = _try_smart_overview()
            catalog.execute(
                """
                INSERT INTO drives(label, mount_path, total_bytes, free_bytes, smart_scan, scanned_at, scan_mode)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(label) DO UPDATE SET
                    mount_path=excluded.mount_path,
                    total_bytes=excluded.total_bytes,
                    free_bytes=excluded.free_bytes,
                    smart_scan=excluded.smart_scan,
                    scanned_at=excluded.scanned_at,
                    scan_mode=excluded.scan_mode
                """,
                (
                    self.label,
                    str(self.mount_path),
                    int(total_bytes),
                    int(free_bytes),
                    smart_blob,
                    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "Full" if self.full_rescan else "Delta",
                ),
            )
            catalog.commit()
        finally:
            catalog.close()
            shard.close()

        print(
            f"[OK] Scan complete for {self.label}. Catalog: {self.catalog_db} | shard: {self.shard_db}",
            flush=True,
        )


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan a drive and populate the VideoCatalog databases.",
    )
    parser.add_argument("--label", help="Drive label to record in the catalog.")
    parser.add_argument("--mount", dest="mount_path", help="Filesystem path where the drive is mounted.")
    parser.add_argument("--catalog-db", dest="catalog_db", help="Optional path to the catalog database (defaults to working dir).")
    parser.add_argument("--shard-db", dest="shard_db", help="Optional path to the shard database (defaults to working dir).")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging output.")
    parser.add_argument("--debug", action="store_true", help="Enable debug helpers such as slow enumeration sleeps.")
    parser.add_argument("--full-rescan", action="store_true", help="Force a full rescan even if files are unchanged.")
    parser.add_argument("--no-resume", action="store_true", help="Disable resume support and clear checkpoints.")
    parser.add_argument(
        "--checkpoint-seconds",
        type=int,
        default=5,
        help="Seconds between checkpoint writes (default: 5).",
    )
    parser.add_argument("--debug-slow-enumeration", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("positional", nargs="*", help=argparse.SUPPRESS)

    namespace = parser.parse_args(argv)
    positional = list(namespace.positional)

    if namespace.label is None and positional:
        namespace.label = positional.pop(0)
    if namespace.mount_path is None and positional:
        namespace.mount_path = positional.pop(0)
    if namespace.catalog_db is None and positional:
        namespace.catalog_db = positional.pop(0)
    if namespace.shard_db is None and positional:
        namespace.shard_db = positional.pop(0)

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


def main(argv: Optional[List[str]] = None) -> int:
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

    scanner = DriveScanner(
        label=args.label,
        mount_path=args.mount_path,
        catalog_db=catalog_db_path,
        shard_db=shard_db_path,
        full_rescan=bool(args.full_rescan),
        resume=not bool(args.no_resume),
        checkpoint_seconds=args.checkpoint_seconds or 5,
        debug_slow=bool(args.debug or args.debug_slow_enumeration),
    )
    scanner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
