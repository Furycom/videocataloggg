import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import hashlib
import importlib
import subprocess

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


@dataclass
class FileInfo:
    path: str
    size_bytes: int
    mtime: float
    mtime_iso: str
    is_av: bool


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


_blake3_spec = importlib.util.find_spec("blake3")
_blake3_hash = importlib.import_module("blake3").blake3 if _blake3_spec is not None else None


class ProgressEmitter:
    def __init__(self) -> None:
        self._last_emit: float = 0.0
        self._start: Optional[float] = None

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
        payload: Dict[str, object] = {
            "type": "progress",
            "phase": phase,
            "elapsed_s": int(now - (self._start or now)),
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
        data = {row[0]: row[1] for row in cur.fetchall()}
        return data

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
        self.catalog_db = Path(catalog_db)
        self.shard_db = Path(shard_db)
        self.full_rescan = full_rescan
        self.resume = resume
        self.checkpoint_seconds = max(1, int(checkpoint_seconds))
        self.debug_slow = debug_slow
        self.emitter = ProgressEmitter()
        self._last_checkpoint = 0.0
        self._state_store: Optional[ScanStateStore] = None
        self._ffmpeg_available = shutil.which("ffmpeg") is not None
        self._mediainfo_available = shutil.which("mediainfo") is not None

    # ----- schema helpers -----
    def _ensure_catalog_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS drives(" \
            "id INTEGER PRIMARY KEY," \
            "label TEXT NOT NULL UNIQUE," \
            "mount_path TEXT NOT NULL," \
            "total_bytes INTEGER," \
            "free_bytes INTEGER," \
            "smart_scan TEXT," \
            "scanned_at TEXT NOT NULL" \
            ")"
        )
        conn.commit()

    def _ensure_shard_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS files(" \
            "id INTEGER PRIMARY KEY," \
            "drive_label TEXT NOT NULL," \
            "path TEXT NOT NULL UNIQUE," \
            "size_bytes INTEGER," \
            "is_av INTEGER DEFAULT 0," \
            "hash_blake3 TEXT," \
            "media_json TEXT," \
            "integrity_ok INTEGER," \
            "mtime_utc TEXT," \
            "deleted INTEGER DEFAULT 0," \
            "deleted_ts TEXT" \
            ")"
        )
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(files)")
        columns = {row[1] for row in cur.fetchall()}
        if "is_av" not in columns:
            cur.execute("ALTER TABLE files ADD COLUMN is_av INTEGER DEFAULT 0")
        if "deleted" not in columns:
            cur.execute("ALTER TABLE files ADD COLUMN deleted INTEGER DEFAULT 0")
        if "deleted_ts" not in columns:
            cur.execute("ALTER TABLE files ADD COLUMN deleted_ts TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_path ON files(path)"
        )
        conn.commit()

    # ----- helpers -----
    def _hash_file(self, file_path: str) -> Optional[str]:
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

    def _mediainfo(self, file_path: str) -> Optional[dict]:
        if not self._mediainfo_available:
            return None
        try:
            out = subprocess.check_output(
                ["mediainfo", "--Output=JSON", file_path],
                stderr=subprocess.STDOUT,
                text=True,
            )
            if out.strip():
                return json.loads(out)
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 127:
                self._mediainfo_available = False
            return {"error": exc.output.strip() if exc.output else "mediainfo_failed"}
        except FileNotFoundError:
            self._mediainfo_available = False
        except Exception as exc:
            return {"error": str(exc)}
        return None

    def _ffmpeg_verify(self, file_path: str) -> Optional[bool]:
        if not self._ffmpeg_available:
            return None
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
            self._ffmpeg_available = False
            return None
        except Exception:
            return False

    def _should_checkpoint(self) -> bool:
        now = time.time()
        if (now - self._last_checkpoint) >= self.checkpoint_seconds:
            self._last_checkpoint = now
            return True
        return False

    # ----- enumeration -----
    def _enumerate_files(self, base: Path) -> List[FileInfo]:
        stack = [base]
        files: List[FileInfo] = []
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
                        mtime_iso = datetime.utcfromtimestamp(stat.st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
                        suffix = Path(entry.name).suffix.lower()
                        is_av = suffix in _AV_EXTS
                        info = FileInfo(
                            path=entry.path,
                            size_bytes=int(stat.st_size),
                            mtime=stat.st_mtime,
                            mtime_iso=mtime_iso,
                            is_av=is_av,
                        )
                        files.append(info)
                        if is_av:
                            total_av += 1
                        now = time.perf_counter()
                        if (now - last_emit) >= 5.0:
                            self.emitter.emit(
                                "enumerating",
                                len(files),
                                total_av,
                            )
                            last_emit = now
                        if self._state_store and self._should_checkpoint():
                            self._state_store.save("enumerating", entry.path)
                        if self.debug_slow:
                            time.sleep(0.01)
            except PermissionError:
                LOGGER.warning("Permission denied while scanning %s", current)
            except FileNotFoundError:
                continue
        files.sort(key=lambda f: f.path)
        self.emitter.emit("enumerating", len(files), total_av, force=True)
        return files

    # ----- change detection -----
    def _load_existing_rows(self, cur: sqlite3.Cursor) -> Dict[str, Dict[str, object]]:
        cur.execute(
            "SELECT path, size_bytes, mtime_utc, deleted, integrity_ok FROM files WHERE drive_label=?",
            (self.label,),
        )
        existing: Dict[str, Dict[str, object]] = {}
        for path, size_bytes, mtime_utc, deleted, integrity in cur.fetchall():
            existing[path] = {
                "size_bytes": size_bytes,
                "mtime_utc": mtime_utc,
                "deleted": int(deleted or 0),
                "integrity_ok": integrity,
            }
        return existing

    # ----- DB helpers -----
    def _mark_deleted(self, cur: sqlite3.Cursor, paths: List[str]) -> None:
        if not paths:
            return
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        cur.executemany(
            "UPDATE files SET deleted=1, deleted_ts=? WHERE path=?",
            [(now, path) for path in paths],
        )

    def _upsert_files(self, cur: sqlite3.Cursor, rows: List[Tuple]) -> None:
        if not rows:
            return
        cur.executemany(
            """
            INSERT INTO files(
                drive_label,
                path,
                size_bytes,
                is_av,
                hash_blake3,
                media_json,
                integrity_ok,
                mtime_utc,
                deleted,
                deleted_ts
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                drive_label=excluded.drive_label,
                size_bytes=excluded.size_bytes,
                is_av=excluded.is_av,
                hash_blake3=excluded.hash_blake3,
                media_json=excluded.media_json,
                integrity_ok=excluded.integrity_ok,
                mtime_utc=excluded.mtime_utc,
                deleted=excluded.deleted,
                deleted_ts=excluded.deleted_ts
            """,
            rows,
        )

    def _update_integrity(self, cur: sqlite3.Cursor, updates: List[Tuple[int, str]]) -> None:
        if not updates:
            return
        cur.executemany(
            "UPDATE files SET integrity_ok=? WHERE path=?",
            updates,
        )

    # ----- main run -----
    def run(self) -> None:
        if not self.mount_path.exists():
            print(f"[ERROR] Mount path not found: {self.mount_path}")
            sys.exit(2)

        missing_tools = []
        if not self._mediainfo_available:
            missing_tools.append("mediainfo")
        if not self._ffmpeg_available:
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

        with sqlite3.connect(self.catalog_db) as catalog:
            self._ensure_catalog_schema(catalog)
            total, _, free = shutil.disk_usage(self.mount_path)
            smart_blob = None
            try:
                proc = subprocess.run(["smartctl", "--scan-open"], capture_output=True, text=True)
                if proc.returncode == 0:
                    smart_blob = proc.stdout
            except Exception:
                smart_blob = None
            catalog.execute(
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
                    self.label,
                    str(self.mount_path.resolve()),
                    int(total),
                    int(free),
                    smart_blob,
                ),
            )
            catalog.commit()

        with sqlite3.connect(self.shard_db) as shard:
            shard.execute("PRAGMA synchronous=NORMAL;")
            self._ensure_shard_schema(shard)
            self._state_store = ScanStateStore(shard)
            if not self.resume:
                self._state_store.clear()
            state = self._state_store.load() if self.resume else {}
            cur = shard.cursor()

            files = self._enumerate_files(self.mount_path)
            total_all = len(files)
            total_av = sum(1 for f in files if f.is_av)

            existing = self._load_existing_rows(cur)
            existing_paths = set(existing.keys())
            disk_paths = {f.path for f in files}

            deleted_paths = [p for p in existing_paths - disk_paths if existing[p]["deleted"] == 0]
            if deleted_paths:
                self._mark_deleted(cur, deleted_paths)
                shard.commit()
                sample = ", ".join(deleted_paths[:3])
                LOGGER.info("Marked %d deleted files (%s%s)", len(deleted_paths), sample, "…" if len(deleted_paths) > 3 else "")
                preview = sample + ("…" if len(deleted_paths) > 3 else "")
                print(
                    f"[INFO] Marked {len(deleted_paths)} deleted files" + (f" ({preview})" if preview else ""),
                    flush=True,
                )

            to_process: List[FileInfo] = []
            unchanged_all = 0
            done_av = 0
            for info in files:
                rec = existing.get(info.path)
                if self.full_rescan:
                    to_process.append(info)
                    continue
                if rec is None or rec.get("deleted"):
                    to_process.append(info)
                    continue
                if rec.get("size_bytes") != info.size_bytes or rec.get("mtime_utc") != info.mtime_iso:
                    to_process.append(info)
                    continue
                unchanged_all += 1
                if info.is_av and rec.get("integrity_ok") in (0, 1):
                    done_av += 1

            to_process.sort(key=lambda f: f.path)

            processed_all = unchanged_all
            if not state:
                self.emitter.emit(
                    "hashing" if to_process else "enumerating",
                    processed_all,
                    done_av,
                    total_all=total_all,
                    total_av=total_av,
                    force=True,
                )

            if state and state.get("phase") == "hashing":
                last_path = state.get("last_path_processed") or ""
                if last_path:
                    to_process = [info for info in to_process if info.path > last_path]
            elif state and state.get("phase") == "ffmpeg" and not to_process:
                to_process = []
                processed_all = total_all
                done_av = total_av

            self.emitter.emit(
                "hashing" if to_process else "mediainfo",
                processed_all,
                done_av,
                total_all=total_all,
                total_av=total_av,
                force=True,
            )

            batch_rows: List[Tuple] = []
            ffmpeg_queue: List[str] = []
            updates_per_commit = 50
            workers = min(32, max(1, (os.cpu_count() or 1) * 2))
            self._last_checkpoint = time.time()

            def submit_files(files_batch: List[FileInfo]) -> None:
                nonlocal processed_all, done_av
                if not files_batch:
                    return
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    future_map = {executor.submit(self._process_file, info): info for info in files_batch}
                    for future in as_completed(future_map):
                        info = future_map[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            LOGGER.error("Worker error on %s: %s", info.path, exc)
                            processed_all += 1
                            if info.is_av:
                                done_av += 1
                            continue
                        processed_all += 1
                        if result:
                            row, needs_ffmpeg, av_inc = result
                            batch_rows.append(row)
                            if needs_ffmpeg:
                                ffmpeg_queue.append(info.path)
                            done_av += av_inc
                        if len(batch_rows) >= updates_per_commit:
                            self._flush_batch(cur, batch_rows)
                        if self._state_store and self._should_checkpoint():
                            self._state_store.save("hashing", info.path)
                        self.emitter.emit(
                            "hashing",
                            processed_all,
                            done_av,
                            total_all=total_all,
                            total_av=total_av,
                        )
                if batch_rows:
                    self._flush_batch(cur, batch_rows)

            submit_files(to_process)

            pending_ffmpeg = self._load_pending_ffmpeg(cur)
            pending_ffmpeg.extend(ffmpeg_queue)
            unique_ffmpeg = sorted({p for p in pending_ffmpeg if p in disk_paths})
            if state and state.get("phase") == "ffmpeg":
                last_path = state.get("last_path_processed") or ""
                if last_path:
                    unique_ffmpeg = [p for p in unique_ffmpeg if p > last_path]
            self.emitter.emit(
                "ffmpeg" if unique_ffmpeg else "mediainfo",
                processed_all,
                done_av,
                total_all=total_all,
                total_av=total_av,
                force=True,
            )

            if unique_ffmpeg:
                with ThreadPoolExecutor(max_workers=min(8, workers)) as executor:
                    future_map = {executor.submit(self._ffmpeg_verify, path): path for path in unique_ffmpeg}
                    updates: List[Tuple[int, str]] = []
                    for future in as_completed(future_map):
                        path = future_map[future]
                        ok = future.result()
                        if ok is None:
                            continue
                        updates.append((1 if ok else 0, path))
                        processed_ffmpeg += 1
                        if len(updates) >= updates_per_commit:
                            self._update_integrity(cur, updates)
                            updates.clear()
                        if self._state_store and self._should_checkpoint():
                            self._state_store.save("ffmpeg", path)
                        self.emitter.emit(
                            "ffmpeg",
                            processed_all,
                            done_av,
                            total_all=total_all,
                            total_av=total_av,
                        )
                    if updates:
                        self._update_integrity(cur, updates)
                shard.commit()

            if self._state_store:
                self._state_store.save("finalizing", "")
            self.emitter.emit(
                "finalizing",
                total_all,
                total_av,
                total_all=total_all,
                total_av=total_av,
                force=True,
            )
            shard.commit()
            if self._state_store:
                self._state_store.clear()

        print(f"[OK] Scan complete for {self.label}. Catalog: {self.catalog_db} | shard: {self.shard_db}")

    def _process_file(self, info: FileInfo) -> Optional[Tuple[Tuple, bool, int]]:
        try:
            hash_value = self._hash_file(info.path)
            media = self._mediainfo(info.path)
            media_json = json.dumps(media, ensure_ascii=False) if isinstance(media, dict) else None
            needs_ffmpeg = False
            av_inc = 0
            integrity = None
            if info.is_av:
                if isinstance(media, dict):
                    av_inc = 1
                    if media.get("error"):
                        integrity = 0
                        needs_ffmpeg = False
                    else:
                        integrity = None
                        needs_ffmpeg = True
                else:
                    needs_ffmpeg = False
                    av_inc = 0
            else:
                integrity = 1
                av_inc = 0
            row = (
                self.label,
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
            return row, needs_ffmpeg, av_inc
        except Exception as exc:
            LOGGER.error("Failed to process %s: %s", info.path, exc)
            error_blob = json.dumps({"error": str(exc)}, ensure_ascii=False)
            row = (
                self.label,
                info.path,
                info.size_bytes,
                1 if info.is_av else 0,
                None,
                error_blob,
                0,
                info.mtime_iso,
                0,
                None,
            )
            return row, False, 1 if info.is_av else 0

    def _flush_batch(self, cur: sqlite3.Cursor, batch_rows: List[Tuple]) -> None:
        if not batch_rows:
            return
        self._upsert_files(cur, batch_rows)
        cur.connection.commit()
        batch_rows.clear()

    def _load_pending_ffmpeg(self, cur: sqlite3.Cursor) -> List[str]:
        cur.execute(
            "SELECT path FROM files WHERE drive_label=? AND deleted=0 AND is_av=1 AND integrity_ok IS NULL",
            (self.label,),
        )
        return [row[0] for row in cur.fetchall()]


def _expand_user_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded)


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
        checkpoint_seconds=int(args.checkpoint_seconds or 5),
        debug_slow=bool(args.debug or args.debug_slow_enumeration),
    )
    scanner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
