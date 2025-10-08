import argparse
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from paths import (
    ensure_working_dir_structure,
    get_catalog_db_path,
    get_shard_db_path,
    resolve_working_dir,
)
from exports import ExportFilters, export_catalog, parse_since
from tools import bootstrap_local_bin, probe_tool

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
    path: str
    size_bytes: int
    mtime_utc: str
    is_av: bool


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

def hash_blake3(file_path: str, chunk: int = 1024*1024) -> str:
    if _blake3_hash is None:
        h = hashlib.sha256()
    else:
        h = _blake3_hash()
    with open(file_path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _iso_from_timestamp(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_av(path: str) -> bool:
    return Path(path).suffix.lower() in AV_EXTS


def _enumerate_files(
    base_path: Path,
    *,
    debug_slow: bool = False,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> List[FileInfo]:
    stack: List[Path] = [base_path]
    results: List[FileInfo] = []
    last_emit = time.monotonic()

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
                    info = FileInfo(
                        path=entry.path,
                        size_bytes=int(stat.st_size),
                        mtime_utc=_iso_from_timestamp(stat.st_mtime),
                        is_av=_is_av(entry.path),
                    )
                    results.append(info)
                    if debug_slow:
                        time.sleep(0.01)
                    if progress_callback and (time.monotonic() - last_emit) >= 5:
                        progress_callback("enumerating", len(results), 0)
                        last_emit = time.monotonic()
        except PermissionError:
            LOGGER.warning("Permission denied while enumerating %s", current)
        except FileNotFoundError:
            continue
    if progress_callback:
        progress_callback("enumerating", len(results), 0)
    results.sort(key=lambda item: item.path)
    return results


def _load_existing(conn: sqlite3.Connection, drive_label: str) -> Dict[str, dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, path, size_bytes, mtime_utc, deleted FROM files WHERE drive_label=?",
        (drive_label,),
    )
    existing: Dict[str, dict] = {}
    for row in cur.fetchall():
        file_id, path, size_bytes, mtime_utc, deleted = row
        existing[path] = {
            "id": file_id,
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
) -> dict:
    mount = Path(mount_path)
    if not mount.exists():
        print(f"[ERROR] Mount path not found: {mount_path}")
        sys.exit(2)

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

    state_store = ScanStateStore(conn, label, interval_seconds=int(checkpoint_seconds))
    if not resume:
        state_store.clear()
        resume_state: Dict[str, str] = {}
    else:
        resume_state = state_store.load()

    def _progress_event(phase: str, count: int, _unused: int) -> None:
        LOGGER.debug("%s — files enumerated: %s", phase, count)
        if resume:
            state_store.checkpoint(phase, None)

    file_infos = _enumerate_files(mount, debug_slow=debug_slow, progress_callback=_progress_event)
    existing_rows = _load_existing(conn, label)

    enumerated_paths = {info.path for info in file_infos}
    stale_paths = [path for path in existing_rows.keys() if path not in enumerated_paths]
    deleted_count, deleted_examples = _mark_deleted(conn, label, deleted_paths=stale_paths)
    if deleted_count:
        LOGGER.info(
            "Marked %s files as deleted (examples: %s)",
            deleted_count,
            ", ".join(deleted_examples) if deleted_examples else "—",
        )

    pending: List[FileInfo] = []
    unchanged: List[FileInfo] = []
    for info in file_infos:
        prev = existing_rows.get(info.path)
        if full_rescan:
            pending.append(info)
        elif prev is None or prev.get("deleted"):
            pending.append(info)
        else:
            prev_size = prev.get("size_bytes")
            prev_mtime = prev.get("mtime_utc")
            if prev_size != info.size_bytes or prev_mtime != info.mtime_utc:
                pending.append(info)
            else:
                unchanged.append(info)

    _restore_active(conn, label, [info.path for info in unchanged])

    resume_index = -1
    if resume and resume_state:
        last_path = resume_state.get("last_path_processed")
        if last_path:
            for idx, info in enumerate(pending):
                if info.path == last_path:
                    resume_index = idx
                    break

    work_list = pending if resume_index < 0 else pending[resume_index + 1 :]
    processed_files = 0 if resume_index < 0 else resume_index + 1
    processed_av = sum(1 for info in pending[: processed_files] if info.is_av)

    LOGGER.info(
        "Enumerated %s files (%s AV). Pending work: %s (unchanged: %s).",
        len(file_infos),
        sum(1 for info in file_infos if info.is_av),
        len(work_list),
        len(unchanged),
    )

    if resume and work_list:
        last_path = pending[processed_files - 1].path if processed_files > 0 else None
        state_store.checkpoint("hashing", last_path, force=True)

    for info in work_list:
        hash_value = None
        media_blob = None
        integrity_ok: Optional[int] = 1
        try:
            hash_value = hash_blake3(info.path)
            metadata = mediainfo_json(info.path, TOOL_PATHS.get("mediainfo")) if info.is_av else None
            if metadata is not None:
                media_blob = json.dumps(metadata, ensure_ascii=False)
                integrity_ok = 0 if metadata.get("error") else 1
            else:
                media_blob = None
                integrity_ok = None if info.is_av else 1
            if info.is_av:
                ok = ffmpeg_verify(info.path, TOOL_PATHS.get("ffmpeg"))
                if ok is False:
                    integrity_ok = 0
        except Exception as exc:
            LOGGER.exception("Failed to process %s", info.path)
            media_blob = json.dumps({"error": str(exc)}, ensure_ascii=False)
            integrity_ok = 0
        _upsert_file(
            conn,
            label,
            info,
            hash_value=hash_value,
            media_blob=media_blob,
            integrity_ok=integrity_ok,
        )
        processed_files += 1
        if info.is_av:
            processed_av += 1
        if resume:
            state_store.checkpoint("hashing", info.path)

    if resume:
        if work_list:
            state_store.checkpoint("hashing", work_list[-1].path, force=True)
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
    conn.close()
    return {
        "total_files": len(file_infos),
        "av_files": sum(1 for info in file_infos if info.is_av),
        "pending": len(work_list),
        "unchanged": len(unchanged),
        "deleted": deleted_count,
        "duration_seconds": duration,
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
    result = scan_drive(
        args.label,
        args.mount_path,
        str(catalog_db_path),
        full_rescan=bool(getattr(args, "full_rescan", False)),
        resume=not bool(getattr(args, "no_resume", False)),
        checkpoint_seconds=checkpoint_seconds,
        debug_slow=debug_flag,
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
