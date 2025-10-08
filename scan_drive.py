import argparse
import logging
import os, sys, sqlite3, json, time, shutil, subprocess, importlib
import importlib.util
from pathlib import Path
from typing import Dict, Optional
import hashlib

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
        mtime_utc TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_files_drive ON files(drive_label);
    CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash_blake3);
    """)
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

def scan_drive(label: str, mount_path: str, db_path: str, debug_slow: bool = False):
    mount = Path(mount_path)
    if not mount.exists():
        print(f"[ERROR] Mount path not found: {mount_path}")
        sys.exit(2)

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
        print("[WARN] smartctl not found. SMART data will be skipped.", file=sys.stderr)

    total, used, free = shutil.disk_usage(mount)
    conn = init_db(db_path)
    c = conn.cursor()

    smart_blob = try_smart_overview(TOOL_PATHS.get("smartctl"))

    c.execute("""INSERT INTO drives(label, mount_path, total_bytes, free_bytes, smart_scan, scanned_at)
                 VALUES(?,?,?,?,?,datetime('now'))""",
              (label, str(mount.resolve()), int(total), int(free), smart_blob, ))
    conn.commit()

    file_paths: list[str] = []
    av_files = 0
    for root, dirs, files in os.walk(mount):
        for fname in files:
            full_path = os.path.join(root, fname)
            file_paths.append(full_path)
            ext = os.path.splitext(fname)[1].lower()
            if ext in AV_EXTS:
                av_files += 1
            if debug_slow:
                time.sleep(0.01)

    for fp in tqdm(file_paths, desc=f"Scanning {label}"):
        try:
            size = os.path.getsize(fp)
            hb3 = hash_blake3(fp)
            mi = mediainfo_json(fp, TOOL_PATHS.get("mediainfo"))
            ok = ffmpeg_verify(fp, TOOL_PATHS.get("ffmpeg"))
            mtime = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(fp)))
            c.execute("""INSERT INTO files(drive_label, path, size_bytes, hash_blake3, media_json, integrity_ok, mtime_utc)
                         VALUES(?,?,?,?,?,?,?)""",
                      (label, fp, int(size), hb3, json.dumps(mi, ensure_ascii=False) if mi else None, int(ok), mtime))
        except Exception as e:
            c.execute("""INSERT INTO files(drive_label, path, size_bytes, hash_blake3, media_json, integrity_ok, mtime_utc)
                         VALUES(?,?,?,?,?,?,?)""",
                      (label, fp, None, None, json.dumps({"error": str(e)}, ensure_ascii=False), 0, None))
    conn.commit()
    conn.close()

    duration = time.perf_counter() - start_time
    LOGGER.info("Scan complete for %s. DB: %s", label, db_path)
    return {
        "total_files": len(file_paths),
        "av_files": av_files,
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
    result = scan_drive(args.label, args.mount_path, str(catalog_db_path), debug_slow=debug_flag)

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
