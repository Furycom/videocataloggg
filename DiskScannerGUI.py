# DiskScannerGUI.py — V8.2 (Yann)
# Requiert: Python 3.10+, mediainfo (CLI), sqlite3 (intégré), smartctl/ffmpeg optionnels, blake3 (pip)
# Dossier base: C:\Users\Administrator\VideoCatalog

import os, sys, json, time, shutil, sqlite3, threading, subprocess, zipfile, queue
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Literal, Optional

from paths import (
    ensure_working_dir_structure,
    get_catalog_db_path,
    get_drive_types_path,
    get_exports_dir,
    get_logs_dir,
    get_scans_dir,
    get_shard_db_path,
    get_shards_dir,
    load_settings,
    resolve_working_dir,
    update_settings,
)
from tools import (
    bootstrap_local_bin,
    get_winget_candidates,
    install_tool_via_winget,
    probe_tool,
    set_manual_tool_path,
    setup_portable_tool,
    winget_available,
)
from tkinter import (
    Tk, Toplevel, StringVar, IntVar, END, N, S, E, W,
    filedialog, messagebox, ttk, Menu, Spinbox
)
from tkinter.scrolledtext import ScrolledText

# ---------------- Config & constantes ----------------
WORKING_DIR_PATH = resolve_working_dir()
ensure_working_dir_structure(WORKING_DIR_PATH)
bootstrap_local_bin(WORKING_DIR_PATH)

def _expand_user_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = WORKING_DIR_PATH / path
    return path

_SETTINGS = load_settings(WORKING_DIR_PATH)
_settings_catalog = _SETTINGS.get("catalog_db") if isinstance(_SETTINGS, dict) else None

if isinstance(_settings_catalog, str) and _settings_catalog.strip():
    DB_DEFAULT_PATH = _expand_user_path(_settings_catalog.strip())
else:
    DB_DEFAULT_PATH = get_catalog_db_path(WORKING_DIR_PATH)

BASE_DIR_PATH = WORKING_DIR_PATH
SCANS_DIR_PATH = get_scans_dir(WORKING_DIR_PATH)
LOGS_DIR_PATH = get_logs_dir(WORKING_DIR_PATH)
EXPORTS_DIR_PATH = get_exports_dir(WORKING_DIR_PATH)
SHARDS_DIR_PATH = get_shards_dir(WORKING_DIR_PATH)
TYPES_JSON_PATH = get_drive_types_path(WORKING_DIR_PATH)

DB_DEFAULT = str(DB_DEFAULT_PATH)
BASE_DIR = str(BASE_DIR_PATH)
SCANS_DIR = str(SCANS_DIR_PATH)
LOGS_DIR = str(LOGS_DIR_PATH)
EXPORTS_DIR = str(EXPORTS_DIR_PATH)
SHARDS_DIR = str(SHARDS_DIR_PATH)
TYPES_JSON = str(TYPES_JSON_PATH)
LOG_FILE = str(LOGS_DIR_PATH / "scanner.log")

update_settings(WORKING_DIR_PATH, catalog_db=DB_DEFAULT)

_STARTUP_INFO = (
    f"Working directory: {WORKING_DIR_PATH} | "
    f"catalog: {DB_DEFAULT_PATH} | shards: {SHARDS_DIR_PATH}"
)
print(f"[INFO] {_STARTUP_INFO}", flush=True)


def shard_path_for(label: str) -> Path:
    return get_shard_db_path(WORKING_DIR_PATH, label)

VIDEO_EXTS = {
    ".mp4",".mkv",".avi",".mov",".wmv",".m4v",".ts",".m2ts",".webm",".mpg",".mpeg",".mp2",".vob",".flv",".3gp",".ogv",".mts",".m2t"
}
AUDIO_EXTS = {
    ".mp3",".flac",".aac",".m4a",".wav",".wma",".ogg",".opus",".alac",".aiff",".ape",".dsf",".dff"
}
AV_EXTS = VIDEO_EXTS | AUDIO_EXTS

ToolName = Literal["mediainfo", "ffmpeg", "smartctl"]
ALL_TOOLS: tuple[ToolName, ...] = ("mediainfo", "ffmpeg", "smartctl")
REQUIRED_TOOLS: tuple[ToolName, ...] = ("mediainfo", "ffmpeg")
TOOL_DISPLAY: Dict[ToolName, str] = {
    "mediainfo": "MediaInfo",
    "ffmpeg": "FFmpeg",
    "smartctl": "smartctl",
}

# ---------------- Utilitaires ----------------
_LOG_LISTENERS: list = []
_LOG_LISTENERS_LOCK = threading.Lock()

def register_log_listener(callback):
    with _LOG_LISTENERS_LOCK:
        _LOG_LISTENERS.append(callback)

def unregister_log_listener(callback):
    with _LOG_LISTENERS_LOCK:
        try:
            _LOG_LISTENERS.remove(callback)
        except ValueError:
            pass

def log(s: str):
    os.makedirs(LOGS_DIR, exist_ok=True)
    line = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ')}] {s}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    listeners = []
    with _LOG_LISTENERS_LOCK:
        listeners = list(_LOG_LISTENERS)
    for cb in listeners:
        try:
            cb(line)
        except Exception:
            pass


log(f"[INFO] {_STARTUP_INFO}")

@lru_cache(maxsize=None)
def has_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def disk_usage_bytes(path: Path):
    total, used, free = shutil.disk_usage(path)
    return int(total), int(free)

def mediainfo_json(file_path: str, executable: Optional[str]) -> Optional[dict]:
    if not executable:
        return None
    try:
        out = subprocess.check_output(
            [executable, "--Output=JSON", file_path],
            stderr=subprocess.STDOUT, text=True, timeout=180
        )
        return json.loads(out)
    except subprocess.TimeoutExpired:
        log(f"[mediainfo timeout] {file_path}")
        return {"error":"mediainfo_timeout"}
    except Exception as e:
        log(f"[mediainfo fail] {file_path} -> {e}")
        return {"error": str(e)}

def blake3_hash(file_path: str, chunk: int = 1024*1024) -> Optional[str]:
    try:
        from blake3 import blake3
    except Exception:
        return None
    h = blake3()
    try:
        with open(file_path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b: break
                h.update(b)
        return h.hexdigest()
    except Exception as e:
        log(f"[hash fail] {file_path} -> {e}")
        return None

def try_smart_overview(executable: Optional[str]) -> Optional[str]:
    if not executable:
        return None
    acc = {"scan": None, "details": []}
    try:
        out = subprocess.check_output([executable, "--scan-open"], text=True, stderr=subprocess.STDOUT)
        acc["scan"] = out
        for n in range(0,20):
            try:
                out = subprocess.check_output(
                    [executable, "-i", "-H", "-A", "-j", fr"\\.\PhysicalDrive{n}"],
                    text=True, stderr=subprocess.STDOUT
                )
                acc["details"].append(json.loads(out))
            except Exception:
                pass
        return json.dumps(acc, ensure_ascii=False)
    except Exception:
        return None

# ---------------- SQLite helpers ----------------
def _table_has_column(cur: sqlite3.Cursor, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

def init_catalog(db_path: str):
    """Crée le schéma si absent ET migre les DB anciennes (ajout de colonnes manquantes)."""
    os.makedirs(Path(db_path).parent, exist_ok=True)
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # Tables minimales
    cur.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS drives(
      id INTEGER PRIMARY KEY,
      label TEXT NOT NULL,
      mount_path TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS jobs(
      id INTEGER PRIMARY KEY,
      drive_label TEXT NOT NULL,
      started_at TEXT NOT NULL,
      status TEXT NOT NULL
    );
    """)
    con.commit()

    # ---- MIGRATION DRIVES ----
    drives_cols = [
        ("fs_format","TEXT"), ("total_bytes","INTEGER"), ("free_bytes","INTEGER"),
        ("smart_scan","TEXT"), ("scanned_at","TEXT"), ("drive_type","TEXT"),
        ("notes","TEXT"), ("scan_mode","TEXT"), ("serial","TEXT"), ("model","TEXT")
    ]
    for name, typ in drives_cols:
        if not _table_has_column(cur, "drives", name):
            cur.execute(f"ALTER TABLE drives ADD COLUMN {name} {typ}")
    con.commit()

    # ---- MIGRATION JOBS ----
    jobs_cols = [
        ("finished_at","TEXT"), ("total_av","INTEGER"), ("total_all","INTEGER"),
        ("done_av","INTEGER"), ("duration_sec","INTEGER"), ("message","TEXT")
    ]
    for name, typ in jobs_cols:
        if not _table_has_column(cur, "jobs", name):
            cur.execute(f"ALTER TABLE jobs ADD COLUMN {name} {typ}")
    con.commit()

    # Index utile
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_drives_label_unique ON drives(label)")
    con.commit()
    con.close()

def _ensure_shard_schema(con: sqlite3.Connection):
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS files(
          id INTEGER PRIMARY KEY,
          path TEXT NOT NULL,
          size_bytes INTEGER,
          is_av INTEGER,
          hash_blake3 TEXT,
          media_json TEXT,
          integrity_ok INTEGER,
          mtime_utc TEXT
        )
    """)

    # Ensure all expected columns exist (backward compatibility with older shards).
    required_columns = [
        ("size_bytes", "INTEGER"),
        ("is_av", "INTEGER"),
        ("hash_blake3", "TEXT"),
        ("media_json", "TEXT"),
        ("integrity_ok", "INTEGER"),
        ("mtime_utc", "TEXT"),
    ]
    cur.execute("PRAGMA table_info(files)")
    existing_columns = {row[1] for row in cur.fetchall()}
    for col_name, col_type in required_columns:
        if col_name not in existing_columns:
            cur.execute(f"ALTER TABLE files ADD COLUMN {col_name} {col_type}")

    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_files_path ON files(path);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash_blake3);")

    # integrity_ok semantics:
    #   1 → MediaInfo/scan succeeded (or non-A/V file)
    #   0 → MediaInfo reported an error for the file
    #   NULL → MediaInfo not executed (tool missing/disabled)
    con.commit()


def init_shard(shard_path: str):
    con = sqlite3.connect(shard_path)
    try:
        _ensure_shard_schema(con)
    finally:
        con.close()

# ---------------- Scanner worker ----------------
class ScannerWorker(threading.Thread):
    def __init__(
        self,
        label: str,
        mount: str,
        notes: str,
        dtype: str,
        db_catalog: str,
        blake_for_av: bool,
        max_workers: int,
        stop_evt: threading.Event,
        event_queue: "queue.Queue[dict]",
        tool_state: Optional[Dict[str, dict]] = None,
    ):
        super().__init__(daemon=False)
        self.label = label
        self.mount = mount
        self.notes = notes
        self.dtype = dtype
        self.db_catalog = db_catalog
        self.blake_for_av = blake_for_av
        self.max_workers = max(1, int(max_workers))
        self.stop_evt = stop_evt
        self.queue = event_queue
        self.job_id: Optional[int] = None
        self._start_monotonic: Optional[float] = None
        self._last_progress_emit: float = 0.0
        self._total_all: int = 0
        self._total_av: int = 0
        self._current_phase: str = "enumerating"
        self._tool_missing_reported = False
        self.tool_state: Dict[str, dict] = tool_state or {}
        self.tool_paths: Dict[str, Optional[str]] = {}
        for tool_name, info in self.tool_state.items():
            path = info.get("path") if isinstance(info, dict) else None
            present = bool(info.get("present")) if isinstance(info, dict) else False
            self.tool_paths[tool_name] = str(path) if (present and path) else None

    def _put(self, payload: dict):
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            pass

    def _emit_progress(
        self,
        phase: str,
        files_seen: int,
        av_seen: int,
        total_all: Optional[int] = None,
        force: bool = False,
    ):
        if self._tool_missing_reported:
            return
        now = time.perf_counter()
        if self._start_monotonic is None:
            self._start_monotonic = now
        if not force and (now - self._last_progress_emit) < 5:
            return
        elapsed = int(now - (self._start_monotonic or now))
        files_total_payload = total_all
        if files_total_payload is None and self._total_all:
            files_total_payload = self._total_all
        total_av_payload = self._total_av or None
        payload = {
            "type": "progress",
            "phase": phase,
            "elapsed_s": elapsed,
            "files_total": files_total_payload,
            "files_seen": files_seen,
            "av_seen": av_seen,
            "done_all": files_seen,
            "done_av": av_seen,
        }
        if files_total_payload is not None:
            payload["total_all"] = files_total_payload
        if total_av_payload is not None:
            payload["total_av"] = total_av_payload
        self._put(payload)
        self._last_progress_emit = now

    def _report_missing_tool(self, tool: str):
        if self._tool_missing_reported:
            return
        self._tool_missing_reported = True
        message = f"Error — {tool} not found. Please install {tool} and try again."
        self._put({"type": "tool_missing", "tool": tool})
        self._put({"type": "done", "status": "Error", "message": message})

    def run(self):
        try:
            self._run()
        except Exception as exc:
            log(f"[ScannerWorker] fatal error: {exc}")
            if self.job_id is not None:
                try:
                    with sqlite3.connect(self.db_catalog) as catalog:
                        catalog.execute(
                            "UPDATE jobs SET status='Error', message=?, finished_at=datetime('now') WHERE id=?",
                            (str(exc), self.job_id)
                        )
                        catalog.commit()
                except Exception as db_exc:
                    log(f"[ScannerWorker] failed to mark job error: {db_exc}")
            self._put({"type": "error", "title": "Scan error", "message": str(exc)})
            self._put({"type": "done", "status": "Error"})

    def _run(self):
        mount = Path(self.mount)
        if not mount.exists():
            self._put({"type": "error", "title": "Scan error", "message": f"Mount path not found: {self.mount}"})
            self._put({"type": "done", "status": "Error"})
            return

        self._start_monotonic = time.perf_counter()
        self._last_progress_emit = (self._start_monotonic or time.perf_counter()) - 6

        shard_path = shard_path_for(self.label)
        shard_path.parent.mkdir(parents=True, exist_ok=True)

        self._put({"type": "status", "message": "Enumerating files…"})

        def iter_files(base: Path):
            for root_dir, _, files in os.walk(base):
                for name in files:
                    yield os.path.join(root_dir, name)

        all_files: list[str] = []
        total_av = 0
        self._current_phase = "enumerating"
        for fp in iter_files(mount):
            if self.stop_evt.is_set():
                break
            all_files.append(fp)
            if Path(fp).suffix.lower() in AV_EXTS:
                total_av += 1
            self._emit_progress(self._current_phase, len(all_files), total_av)
        self._total_all = len(all_files)
        self._total_av = total_av
        self._emit_progress(self._current_phase, self._total_all, total_av, total_all=self._total_all, force=True)
        total_all = self._total_all
        if self.stop_evt.is_set():
            self._put({"type": "status", "message": "Scan stopped."})
            self._put({"type": "done", "status": "Stopped"})
            return

        started_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        smart_blob = try_smart_overview(self.tool_paths.get("smartctl"))
        total_bytes, free_bytes = disk_usage_bytes(mount)

        with sqlite3.connect(self.db_catalog) as catalog:
            ccur = catalog.cursor()
            ccur.execute("""
            INSERT INTO drives(label, mount_path, fs_format, total_bytes, free_bytes, smart_scan, scanned_at, drive_type, notes, scan_mode)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(label) DO UPDATE SET
                mount_path=excluded.mount_path, fs_format=excluded.fs_format, total_bytes=excluded.total_bytes,
                free_bytes=excluded.free_bytes, smart_scan=excluded.smart_scan, scanned_at=excluded.scanned_at,
                drive_type=excluded.drive_type, notes=excluded.notes, scan_mode=excluded.scan_mode
            """, (self.label, str(mount), None, total_bytes, free_bytes, smart_blob,
                  started_at, self.dtype, self.notes, "Auto"))

            ccur.execute("""INSERT INTO jobs(drive_label, started_at, status, total_av, total_all, done_av, duration_sec, message)
                            VALUES(?, datetime('now'), 'Running', ?, ?, 0, NULL, ?)""",
                         (self.label, total_av, total_all, f"Found AV {total_av} / Total {total_all}"))
            self.job_id = ccur.lastrowid
            catalog.commit()

            log(f"[Job {self.job_id}] Found AV {total_av} of TOTAL {total_all} at {self.mount}")
            self._put({"type": "refresh_jobs"})

            init_shard(str(shard_path))
            self._put({"type": "status", "message": "Scanning files…"})

            t0 = time.perf_counter()
            done_av = 0
            processed_all = 0
            batch = []
            BATCH_SIZE = 200
            max_workers = max(1, min(self.max_workers, 32))
            log(f"[Job {self.job_id}] scanning with {max_workers} worker thread(s)")
            self._current_phase = "hashing" if self.blake_for_av else "mediainfo"
            self._emit_progress(self._current_phase, processed_all, done_av, total_all=total_all, force=True)

            with sqlite3.connect(str(shard_path)) as shard:
                _ensure_shard_schema(shard)
                scur = shard.cursor()

                existing_rows: dict[str, dict] = {}
                scur.execute("SELECT path, size_bytes, is_av, integrity_ok, mtime_utc FROM files")
                for path, size_bytes, is_av, integrity_ok, mtime_utc in scur.fetchall():
                    existing_rows[path] = {
                        "size_bytes": size_bytes,
                        "is_av": int(is_av or 0),
                        "integrity_ok": integrity_ok,
                        "mtime_utc": mtime_utc,
                    }

                stale_paths = [p for p in list(existing_rows.keys()) if p not in all_files]
                if stale_paths:
                    scur.executemany("DELETE FROM files WHERE path=?", [(p,) for p in stale_paths])
                    shard.commit()
                    for p in stale_paths:
                        existing_rows.pop(p, None)

                def flush():
                    if not batch:
                        return
                    scur.executemany(
                        """
                        INSERT INTO files(path,size_bytes,is_av,hash_blake3,media_json,integrity_ok,mtime_utc)
                        VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(path) DO UPDATE SET
                            size_bytes=excluded.size_bytes,
                            is_av=excluded.is_av,
                            hash_blake3=excluded.hash_blake3,
                            media_json=excluded.media_json,
                            integrity_ok=excluded.integrity_ok,
                            mtime_utc=excluded.mtime_utc
                        """,
                        batch,
                    )
                    shard.commit()
                    batch.clear()

                def handle_result(result):
                    nonlocal processed_all, done_av
                    if not result:
                        return
                    if isinstance(result, tuple) and result and result[0] == "skip":
                        _, av_inc = result
                        processed_all += 1
                        if av_inc:
                            done_av += av_inc
                    else:
                        row, av_inc = result
                        if row is None:
                            return
                        batch.append(row)
                        if len(batch) >= BATCH_SIZE:
                            flush()
                        processed_all += 1
                        if av_inc:
                            done_av += av_inc
                    self._emit_progress(
                        self._current_phase,
                        processed_all,
                        done_av,
                        total_all=total_all,
                        force=(processed_all % 200 == 0 or processed_all == total_all),
                    )

                def process_file(fp: str):
                    if self.stop_evt.is_set():
                        return None
                    try:
                        p = Path(fp)
                        ext = p.suffix.lower()
                        st = p.stat()
                        size = st.st_size
                        mtime = datetime.utcfromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
                        prev = existing_rows.get(fp)
                        if prev and prev.get("size_bytes") == size and prev.get("mtime_utc") == mtime:
                            av_inc = 1 if prev.get("is_av") == 1 and prev.get("integrity_ok") in (0, 1) else 0
                            return ("skip", av_inc)
                        mediainfo_path = self.tool_paths.get("mediainfo")
                        if ext in AV_EXTS:
                            mi = mediainfo_json(fp, mediainfo_path)
                            hb3 = blake3_hash(fp) if self.blake_for_av else None
                            if mi is None:
                                ok = None
                                media_blob = None
                            else:
                                ok = 1 if "error" not in mi else 0
                                media_blob = json.dumps(mi, ensure_ascii=False)
                            return ((fp, size, 1, hb3, media_blob, ok, mtime), 1 if ok in (0, 1) else 0)
                        return ((fp, size, 0, None, None, 1, mtime), 0)
                    except Exception as e:
                        log(f"[job {self.job_id}] error on {fp}: {e}")
                        return ((fp, None, 0, None, json.dumps({"error": str(e)}, ensure_ascii=False), 0, None), 0)

                if max_workers > 1 and len(all_files) > 1:
                    def _iter_inputs():
                        for fp in all_files:
                            if self.stop_evt.is_set():
                                break
                            yield fp

                    executor = ThreadPoolExecutor(max_workers=max_workers)
                    try:
                        for result in executor.map(process_file, _iter_inputs(), chunksize=8):
                            handle_result(result)
                            if self.stop_evt.is_set():
                                break
                    finally:
                        executor.shutdown(wait=False, cancel_futures=True)
                else:
                    for fp in all_files:
                        if self.stop_evt.is_set():
                            break
                        handle_result(process_file(fp))

                flush()

            self._current_phase = "finalizing"
            self._emit_progress(self._current_phase, processed_all, done_av, total_all=total_all, force=True)

            duration = int(time.perf_counter() - t0)
            status = "Stopped" if self.stop_evt.is_set() else "Done"
            ccur.execute("""UPDATE jobs SET finished_at=datetime('now'), status=?, done_av=?, duration_sec=?, message=?
                            WHERE id=?""",
                         (status, done_av, duration, f"Completed ({done_av}/{total_av}); scanned {total_all} files total", self.job_id))
            catalog.commit()

            self._put({"type": "refresh_jobs"})
            final_msg = "Scan stopped." if self.stop_evt.is_set() else "Scan complete."
            self._put({"type": "status", "message": final_msg})
            self._put({
                "type": "done",
                "status": status,
                "total_all": total_all,
                "done_av": done_av,
                "total_av": self._total_av,
                "duration": duration,
            })

# ---------------- GUI ----------------
class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Disk Scanner - Catalog GUI")
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self.colors = self._setup_theme()
        self.banner_styles = {
            "INFO": {"frame": "BannerINFO.TFrame", "label": "BannerINFO.TLabel"},
            "SUCCESS": {"frame": "BannerSUCCESS.TFrame", "label": "BannerSUCCESS.TLabel"},
            "WARNING": {"frame": "BannerWARNING.TFrame", "label": "BannerWARNING.TLabel"},
            "ERROR": {"frame": "BannerERROR.TFrame", "label": "BannerERROR.TLabel"},
        }
        self.status_line_active_style = "StatusLineActive.TLabel"
        self.status_line_idle_style = "StatusLineIdle.TLabel"
        self.status_line_idle_text = "Ready."
        self.banner_after_id: Optional[str] = None
        self.status_line_update_id: Optional[str] = None
        self.scan_in_progress = False
        self.scan_start_ts: Optional[float] = None
        self.last_progress_ts: Optional[float] = None
        self.progress_snapshot: Optional[dict] = None
        self.heartbeat_active = False
        self.activity_indicator_running = False
        self._banner_visible = False
        self.tool_status: Dict[str, dict] = {}
        self._diagnostics_windows: List["DiagnosticsDialog"] = []
        self._tool_banner_active = False
        self.status_styles = {
            "info": "StatusInfo.TLabel",
            "success": "StatusSuccess.TLabel",
            "warning": "StatusWarning.TLabel",
            "error": "StatusDanger.TLabel",
            "accent": "StatusAccent.TLabel",
        }
        self.root.configure(bg=self.colors["background"])
        self.root.option_add("*Font", "Segoe UI 10")
        self.root.geometry("1020x820")
        self.root.minsize(900, 720)
        self.db_path = StringVar(value=DB_DEFAULT)

        # inputs
        self.path_var  = StringVar()
        self.label_var = StringVar()
        self.type_var  = StringVar()
        self.notes_var = StringVar()
        self.blake_var = IntVar(value=0)
        cpu_count = os.cpu_count() or 1
        default_threads = min(8, max(1, (cpu_count // 2) or 1))
        self.threads_var = IntVar(value=default_threads)
        self.log_lines: list[str] = []

        # worker state
        self.stop_evt: Optional[threading.Event] = None
        self.worker: Optional[ScannerWorker] = None
        self.worker_queue: "queue.Queue[dict]" = queue.Queue()
        self._closing = False

        self._build_menu()
        self._build_form()
        self._build_tables()

        self.refresh_tool_statuses(initial=True)

        register_log_listener(self._log_enqueue)
        self._load_existing_logs()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._poll_worker_queue)

        for d in (SCANS_DIR, LOGS_DIR, EXPORTS_DIR, SHARDS_DIR):
            os.makedirs(d, exist_ok=True)
        init_catalog(self.db_path.get())
        self.refresh_all()
        self._update_status_line(force=True)
        self._schedule_status_ticker()

    def _probe_all_tools(self) -> Dict[str, dict]:
        statuses: Dict[str, dict] = {}
        for tool in ALL_TOOLS:
            try:
                statuses[tool] = probe_tool(tool, WORKING_DIR_PATH)
            except Exception as exc:
                statuses[tool] = {
                    "name": tool,
                    "present": False,
                    "version": None,
                    "path": None,
                    "errors": [str(exc)],
                }
        return statuses

    def _log_tool_statuses(self, statuses: Dict[str, dict]):
        parts = []
        for tool in ALL_TOOLS:
            info = statuses.get(tool) or {}
            present = "present" if info.get("present") else "missing"
            version = info.get("version") or "n/a"
            path = info.get("path") or "n/a"
            parts.append(f"{TOOL_DISPLAY[tool]}: {present}, version={version}, path={path}")
        line = "Tool status — " + "; ".join(parts)
        print(f"[INFO] {line}", flush=True)
        log(f"[INFO] {line}")

    def _notify_diagnostics(self, statuses: Dict[str, dict]):
        alive: List["DiagnosticsDialog"] = []
        for window in list(self._diagnostics_windows):
            if window.winfo_exists():
                window.update_rows(statuses)
                alive.append(window)
        self._diagnostics_windows = alive

    def _update_tool_banner(self):
        missing_required = [
            TOOL_DISPLAY[name]
            for name in REQUIRED_TOOLS
            if not self.tool_status.get(name, {}).get("present")
        ]
        if missing_required:
            message = (
                ", ".join(missing_required)
                + " missing. Open Tools ▸ Diagnostics to install or locate tools."
            )
            if not self._banner_visible or self._tool_banner_active:
                self.show_banner(message, "WARNING")
                self._tool_banner_active = True
        else:
            if self._tool_banner_active and self._banner_visible and not self.scan_in_progress:
                self.clear_banner()
            self._tool_banner_active = False

    def _update_tool_statuses(self, statuses: Dict[str, dict], *, log_line: bool = False):
        self.tool_status = statuses
        if log_line:
            self._log_tool_statuses(statuses)
        self._update_tool_banner()
        self._notify_diagnostics(statuses)

    def refresh_tool_statuses(self, initial: bool = False):
        statuses = self._probe_all_tools()
        self._update_tool_statuses(statuses, log_line=initial)

    def recheck_tool(self, tool: ToolName) -> dict:
        statuses = self._probe_all_tools()
        self._update_tool_statuses(statuses)
        return statuses.get(tool, {})

    def open_diagnostics_dialog(self):
        for window in list(self._diagnostics_windows):
            if window.winfo_exists():
                window.lift()
                window.focus_set()
                return
        window = DiagnosticsDialog(self)
        self._diagnostics_windows.append(window)

    def open_install_dialog(self, tool: ToolName):
        return ToolInstallDialog(self, tool)

    def locate_tool_manually(self, tool: ToolName):
        display = TOOL_DISPLAY[tool]
        current_path = self.tool_status.get(tool, {}).get("path") if self.tool_status.get(tool) else None
        base_dir = Path(current_path).parent if current_path else Path(BASE_DIR)
        file_path = filedialog.askopenfilename(
            title=f"Locate {display} executable",
            initialdir=str(base_dir),
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if not file_path:
            return
        success, message = set_manual_tool_path(tool, file_path, WORKING_DIR_PATH)
        if success:
            log(f"Using manual {display} at {message}")
            messagebox.showinfo("Tool path saved", f"Saved {display} path.\n{message}")
        else:
            messagebox.showerror("Locate tool", message)
        self.refresh_tool_statuses()

    def _prompt_tool_decision(self, tool: ToolName) -> Optional[str]:
        display = TOOL_DISPLAY[tool]
        top = Toplevel(self.root)
        top.title("Required tool missing")
        top.transient(self.root)
        top.grab_set()
        top.resizable(False, False)
        frame = ttk.Frame(top, padding=(18, 18))
        frame.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        ttk.Label(
            frame,
            text=f"{display} is not available. Install now or continue with limited features?",
            wraplength=360,
            style="Subtle.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        result = {"value": None}

        def _set(value: Optional[str]):
            result["value"] = value
            top.destroy()

        btn_install = ttk.Button(frame, text="Install", style="Accent.TButton", command=lambda: _set("install"))
        btn_install.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        ttk.Button(
            frame,
            text=f"Continue without {display}",
            command=lambda: _set("continue"),
        ).grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(16, 0))
        ttk.Button(frame, text="Cancel", command=lambda: _set(None)).grid(row=1, column=2, sticky="ew", pady=(16, 0))
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)

        self.root.wait_window(top)
        return result["value"]

    def _ensure_tools_before_scan(self) -> Optional[Dict[str, dict]]:
        statuses = self._probe_all_tools()
        self._update_tool_statuses(statuses)
        for tool in REQUIRED_TOOLS:
            info = statuses.get(tool)
            if info and not info.get("present"):
                while True:
                    choice = self._prompt_tool_decision(tool)
                    if choice == "install":
                        dialog = self.open_install_dialog(tool)
                        if dialog is not None:
                            self.root.wait_window(dialog)
                        statuses = self._probe_all_tools()
                        self._update_tool_statuses(statuses)
                        if statuses.get(tool, {}).get("present"):
                            break
                        messagebox.showwarning(
                            "Install failed",
                            f"{TOOL_DISPLAY[tool]} is still missing. Retry installation or continue without it.",
                        )
                        continue
                    if choice == "continue":
                        break
                    return None
        return statuses

    # ----- UI builders
    def _setup_theme(self) -> dict[str, str]:
        colors = {
            "background": "#0f172a",
            "content": "#111f33",
            "card": "#15273f",
            "border": "#1f324d",
            "text": "#e2e8f0",
            "muted_text": "#94a3b8",
            "accent": "#38bdf8",
            "accent_dark": "#0ea5e9",
            "success": "#4ade80",
            "warning": "#facc15",
            "danger": "#f97316",
            "info": "#3b82f6"
        }

        self.style.configure("TFrame", background=colors["content"])
        self.style.configure("Content.TFrame", background=colors["content"])
        self.style.configure("Header.TFrame", background=colors["background"])
        self.style.configure("Card.TFrame", background=colors["card"], borderwidth=0)
        self.style.configure("Card.TLabelframe", background=colors["card"], foreground=colors["text"], borderwidth=0)
        self.style.configure("Card.TLabelframe.Label", background=colors["card"], foreground=colors["muted_text"], font=("Segoe UI", 10, "bold"))
        self.style.configure("HeaderTitle.TLabel", background=colors["background"], foreground=colors["text"], font=("Segoe UI", 18, "bold"))
        self.style.configure("HeaderSubtitle.TLabel", background=colors["background"], foreground=colors["muted_text"], font=("Segoe UI", 11))
        self.style.configure("TLabel", background=colors["card"], foreground=colors["text"])
        self.style.configure("Subtle.TLabel", background=colors["card"], foreground=colors["muted_text"], font=("Segoe UI", 9))
        self.style.configure("Value.TLabel", background=colors["card"], foreground=colors["accent"], font=("Segoe UI", 10, "bold"))
        self.style.configure("ProgressBig.TLabel", background=colors["card"], foreground=colors["text"], font=("Segoe UI", 24, "bold"))
        self.style.configure("TreeHeading.TLabel", background=colors["card"], foreground=colors["text"], font=("Segoe UI", 10, "bold"))

        button_padding = (14, 8)
        self.style.configure("TButton", padding=button_padding, background=colors["card"], foreground=colors["text"], borderwidth=0)
        self.style.map("TButton", background=[("pressed", colors["accent_dark"]), ("active", colors["accent"])] , foreground=[("disabled", colors["muted_text"])])
        self.style.configure("Accent.TButton", padding=button_padding, background=colors["accent"], foreground=colors["background"], borderwidth=0)
        self.style.map("Accent.TButton", background=[("pressed", colors["accent_dark"]), ("active", colors["accent"])] , foreground=[("disabled", colors["muted_text"])])
        self.style.configure("Danger.TButton", padding=button_padding, background=colors["danger"], foreground=colors["background"], borderwidth=0)
        self.style.map("Danger.TButton", background=[("pressed", "#ea580c"), ("active", colors["danger"])], foreground=[("disabled", colors["muted_text"])])

        # Treeview styling
        self.style.configure("Card.Treeview", background=colors["card"], fieldbackground=colors["card"], foreground=colors["text"], bordercolor=colors["border"], borderwidth=0, rowheight=24)
        self.style.map("Card.Treeview", background=[("selected", colors["accent"])] , foreground=[("selected", colors["background"])])
        self.style.configure("Card.Treeview.Heading", background=colors["border"], foreground=colors["text"], relief="flat")

        # Progressbar styles
        trough = colors["content"]
        self.style.configure("Idle.Horizontal.TProgressbar", troughcolor=trough, background=colors["border"], bordercolor=colors["border"], lightcolor=colors["border"], darkcolor=colors["border"])
        self.style.configure("Info.Horizontal.TProgressbar", troughcolor=trough, background=colors["info"], bordercolor=colors["info"], lightcolor=colors["info"], darkcolor=colors["info"])
        self.style.configure("Accent.Horizontal.TProgressbar", troughcolor=trough, background=colors["accent"], bordercolor=colors["accent"], lightcolor=colors["accent"], darkcolor=colors["accent"])
        self.style.configure("Success.Horizontal.TProgressbar", troughcolor=trough, background=colors["success"], bordercolor=colors["success"], lightcolor=colors["success"], darkcolor=colors["success"])
        self.style.configure("Warning.Horizontal.TProgressbar", troughcolor=trough, background=colors["warning"], bordercolor=colors["warning"], lightcolor=colors["warning"], darkcolor=colors["warning"])

        # Status badge styles
        badge_font = ("Segoe UI", 10, "bold")
        for style_name, bg, fg in (
            ("StatusInfo.TLabel", colors["info"], colors["background"]),
            ("StatusSuccess.TLabel", colors["success"], colors["background"]),
            ("StatusWarning.TLabel", colors["warning"], colors["background"]),
            ("StatusDanger.TLabel", colors["danger"], colors["background"]),
            ("StatusAccent.TLabel", colors["accent"], colors["background"]),
        ):
            self.style.configure(style_name, background=bg, foreground=fg, font=badge_font, padding=(12, 4))

        # Status banner styles
        for level, color in (
            ("INFO", colors["info"]),
            ("SUCCESS", colors["success"]),
            ("WARNING", colors["warning"]),
            ("ERROR", colors["danger"]),
        ):
            self.style.configure(f"Banner{level}.TFrame", background=color)
            self.style.configure(
                f"Banner{level}.TLabel",
                background=color,
                foreground=colors["background"],
                font=("Segoe UI", 11, "bold"),
                padding=(8, 6),
            )

        # Status line styles
        base_font = ("Segoe UI", 10)
        self.style.configure(
            "StatusLineActive.TLabel",
            background=colors["card"],
            foreground=colors["text"],
            font=base_font,
        )
        self.style.configure(
            "StatusLineIdle.TLabel",
            background=colors["card"],
            foreground=colors["muted_text"],
            font=base_font,
        )
        self.style.configure(
            "Heartbeat.TLabel",
            background=colors["card"],
            foreground=colors["warning"],
            font=("Segoe UI", 10, "bold"),
        )

        return colors

    def _build_menu(self):
        menubar = Menu(self.root); self.root.config(menu=menubar)
        mdb = Menu(menubar, tearoff=0)
        mdb.add_command(label="New catalog DB…", command=self.db_new)
        mdb.add_command(label="Open catalog DB…", command=self.db_open)
        mdb.add_separator()
        mdb.add_command(label="Reset (wipe current DB)", command=self.db_reset)
        mdb.add_command(label="Open DB folder", command=lambda: os.startfile(BASE_DIR))
        mdb.add_command(label="VACUUM current DB", command=self.db_vacuum)
        menubar.add_cascade(label="Database", menu=mdb)

        mtools = Menu(menubar, tearoff=0)
        mtools.add_command(label="Export Current Catalog DB", command=self.export_db)
        mtools.add_separator()
        mtools.add_command(label="Diagnostics…", command=self.open_diagnostics_dialog)
        menubar.add_cascade(label="Tools", menu=mtools)

    def _build_form(self):
        self.root.grid_rowconfigure(0, weight=0)
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self.banner_container = ttk.Frame(self.root, style="Header.TFrame")
        self.banner_container.grid(row=0, column=0, sticky="ew")
        self.banner_container.columnconfigure(0, weight=1)

        self.banner_message = StringVar(value="")
        self.banner_frame = ttk.Frame(self.banner_container, style="BannerINFO.TFrame")
        self.banner_frame.grid(row=0, column=0, sticky="ew")
        self.banner_frame.columnconfigure(0, weight=1)
        self.banner_label = ttk.Label(
            self.banner_frame,
            textvariable=self.banner_message,
            anchor="center",
            style="BannerINFO.TLabel",
        )
        self.banner_label.grid(row=0, column=0, sticky="ew", padx=4, pady=2)
        self.banner_frame.grid_remove()

        self.content = ttk.Frame(self.root, padding=(20, 18), style="Content.TFrame")
        self.content.grid(row=1, column=0, sticky=(N, S, E, W))
        self.content.columnconfigure(0, weight=1)

        header = ttk.Frame(self.content, style="Header.TFrame", padding=(0, 0, 0, 16))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Disk Scanner Catalog", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Manage your drives and follow scans at a glance.", style="HeaderSubtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))

        db_frame = ttk.LabelFrame(self.content, text="Database", padding=(16, 12), style="Card.TLabelframe")
        db_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for c in range(4):
            db_frame.columnconfigure(c, weight=0)
        db_frame.columnconfigure(1, weight=1)
        ttk.Label(db_frame, text="Current DB:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Label(db_frame, textvariable=self.db_path, style="Value.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Button(db_frame, text="Browse…", command=self.db_open, style="Accent.TButton").grid(row=0, column=2, sticky="e", padx=(12, 0))
        ttk.Button(db_frame, text="New DB…", command=self.db_new).grid(row=0, column=3, sticky="e", padx=(8, 0))

        scan_frame = ttk.LabelFrame(self.content, text="Scan parameters", padding=(16, 16), style="Card.TLabelframe")
        scan_frame.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        for c in range(4):
            scan_frame.columnconfigure(c, weight=1 if c in (1, 2) else 0)

        ttk.Label(scan_frame, text="Mount or network path:").grid(row=0, column=0, sticky="w", pady=4)
        path_entry = ttk.Entry(scan_frame, textvariable=self.path_var, width=48)
        path_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(12, 12))
        ttk.Button(scan_frame, text="Browse…", command=self.choose_path).grid(row=0, column=3, sticky="ew")

        ttk.Label(scan_frame, text="Disk label:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(scan_frame, textvariable=self.label_var, width=28).grid(row=1, column=1, sticky="ew", padx=(12, 12))
        ttk.Label(scan_frame, text="Drive type:").grid(row=1, column=2, sticky="w", pady=4)
        self.presets = self._load_types()
        self.presets_combo = ttk.Combobox(scan_frame, values=self.presets, textvariable=self.type_var, width=24)
        self.presets_combo.grid(row=1, column=3, sticky="ew")

        presets_row = ttk.Frame(scan_frame, style="Card.TFrame")
        presets_row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        presets_row.columnconfigure(0, weight=1)
        presets_row.columnconfigure(1, weight=1)
        ttk.Button(presets_row, text="Reload presets", command=self._reload_presets).grid(row=0, column=0, sticky="ew")
        ttk.Button(presets_row, text="Save current type", command=self._save_current_type).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(scan_frame, text="Notes:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(scan_frame, textvariable=self.notes_var).grid(row=3, column=1, columnspan=3, sticky="ew", padx=(12, 0))

        ttk.Label(scan_frame, text="Worker threads:").grid(row=4, column=0, sticky="w", pady=4)
        threads_spin = Spinbox(scan_frame, from_=1, to=max(32, (os.cpu_count() or 8)), textvariable=self.threads_var, width=6, justify="center")
        threads_spin.configure(bg=self.colors["content"], fg=self.colors["text"], foreground=self.colors["text"],
                               insertbackground=self.colors["text"], highlightthickness=0, relief="flat")
        threads_spin.grid(row=4, column=1, sticky="w", padx=(12, 12))

        ttk.Checkbutton(scan_frame, text="BLAKE3 hashing for A/V (slower)", variable=self.blake_var).grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 12))

        actions = ttk.Frame(scan_frame, style="Card.TFrame")
        actions.grid(row=6, column=0, columnspan=4, sticky="ew")
        for c in range(4):
            actions.columnconfigure(c, weight=1)
        ttk.Button(actions, text="Scan", command=self.start_scan, style="Accent.TButton").grid(row=0, column=0, sticky="ew")
        ttk.Button(actions, text="Rescan (delete shard)", command=self.rescan).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(actions, text="Stop", command=self.stop_scan, style="Danger.TButton").grid(row=0, column=2, sticky="ew", padx=(8, 0))
        ttk.Button(actions, text="Export catalog", command=self.export_db).grid(row=0, column=3, sticky="ew", padx=(8, 0))

        status_activity = ttk.Frame(scan_frame, style="Card.TFrame")
        status_activity.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        status_activity.columnconfigure(1, weight=1)

        self.activity_indicator = ttk.Progressbar(
            status_activity,
            orient="horizontal",
            mode="indeterminate",
            style="Info.Horizontal.TProgressbar",
            length=180,
        )
        self.activity_indicator.grid(row=0, column=0, sticky="w")
        self.activity_indicator.grid_remove()

        self.status_line_var = StringVar(value="Ready.")
        self.status_line_label = ttk.Label(
            status_activity,
            textvariable=self.status_line_var,
            style=self.status_line_idle_style,
        )
        self.status_line_label.grid(row=0, column=1, sticky="w", padx=(12, 0))

    def _build_tables(self):
        self.content.rowconfigure(3, weight=1)
        self.content.rowconfigure(4, weight=1)
        self.content.rowconfigure(6, weight=1)

        cols = ("id","label","mount","type","notes","serial","model","totalGB")
        drives_frame = ttk.LabelFrame(self.content, text="Drives in catalog", padding=(16, 12), style="Card.TLabelframe")
        drives_frame.grid(row=3, column=0, sticky=(N, S, E, W), pady=(0, 12))
        drives_frame.columnconfigure(0, weight=1)
        drives_frame.rowconfigure(0, weight=1)

        drives_container = ttk.Frame(drives_frame, style="Card.TFrame")
        drives_container.grid(row=0, column=0, sticky=(N, S, E, W))
        drives_container.columnconfigure(0, weight=1)
        drives_container.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(drives_container, columns=cols, show="headings", height=9, style="Card.Treeview")
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, anchor=W, stretch=True)
        tree_scroll = ttk.Scrollbar(drives_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.grid(row=0, column=0, sticky=(N, S, E, W))
        tree_scroll.grid(row=0, column=1, sticky=(N, S))

        drives_actions = ttk.Frame(drives_frame, style="Card.TFrame")
        drives_actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for i in range(3):
            drives_actions.columnconfigure(i, weight=1)
        ttk.Button(drives_actions, text="Delete selected drive", command=self.del_drive, style="Danger.TButton").grid(row=0, column=0, sticky="ew")
        ttk.Button(drives_actions, text="Update selected drive", command=self.update_drive).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(drives_actions, text="Open shard", command=self.open_shard_selected).grid(row=0, column=2, sticky="ew", padx=(8, 0))

        jcols=("id","drive_label","status","done_av","total_av","total_all","started_at","finished_at","duration","message")
        jobs_frame = ttk.LabelFrame(self.content, text="Scan jobs", padding=(16, 12), style="Card.TLabelframe")
        jobs_frame.grid(row=4, column=0, sticky=(N, S, E, W), pady=(0, 12))
        jobs_frame.columnconfigure(0, weight=1)
        jobs_frame.rowconfigure(0, weight=1)

        jobs_container = ttk.Frame(jobs_frame, style="Card.TFrame")
        jobs_container.grid(row=0, column=0, sticky=(N, S, E, W))
        jobs_container.columnconfigure(0, weight=1)
        jobs_container.rowconfigure(0, weight=1)

        self.jobs = ttk.Treeview(jobs_container, columns=jcols, show="headings", height=7, style="Card.Treeview")
        for c in jcols:
            self.jobs.heading(c, text=c)
            anchor = W if c not in ("id","done_av","total_av","total_all") else E
            self.jobs.column(c, anchor=anchor, stretch=True)
        jobs_scroll = ttk.Scrollbar(jobs_container, orient="vertical", command=self.jobs.yview)
        self.jobs.configure(yscrollcommand=jobs_scroll.set)
        self.jobs.grid(row=0, column=0, sticky=(N, S, E, W))
        jobs_scroll.grid(row=0, column=1, sticky=(N, S))

        job_actions = ttk.Frame(jobs_frame, style="Card.TFrame")
        job_actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for i in range(3):
            job_actions.columnconfigure(i, weight=1)
        ttk.Button(job_actions, text="Resume selected job", command=self.resume_job, style="Accent.TButton").grid(row=0, column=0, sticky="ew")
        ttk.Button(job_actions, text="Delete selected job", command=self.del_job, style="Danger.TButton").grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(job_actions, text="Refresh jobs", command=self.refresh_jobs).grid(row=0, column=2, sticky="ew", padx=(8, 0))

        progress_frame = ttk.LabelFrame(self.content, text="Progress", padding=(16, 16), style="Card.TLabelframe")
        progress_frame.grid(row=5, column=0, sticky="ew", pady=(0, 12))
        progress_frame.columnconfigure(0, weight=1)

        header = ttk.Frame(progress_frame, style="Card.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)

        self.progress_percent_var = StringVar(value="0%")
        ttk.Label(header, textvariable=self.progress_percent_var, style="ProgressBig.TLabel").grid(row=0, column=0, sticky="w")

        self.status_var = StringVar()
        self.status_label = ttk.Label(header, textvariable=self.status_var, style="StatusInfo.TLabel")
        self.status_label.grid(row=0, column=1, sticky="e")

        bars = ttk.Frame(progress_frame, style="Card.TFrame")
        bars.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        bars.columnconfigure(1, weight=1)

        self.total_percent_var = StringVar(value="0%")
        ttk.Label(bars, text="All files", style="Subtle.TLabel").grid(row=0, column=0, sticky="w")
        self.total_progress = ttk.Progressbar(bars, orient="horizontal", mode="determinate", style="Idle.Horizontal.TProgressbar")
        self.total_progress.grid(row=0, column=1, sticky="ew", padx=(12, 12))
        ttk.Label(bars, textvariable=self.total_percent_var, style="Subtle.TLabel").grid(row=0, column=2, sticky="e")

        self.av_percent_var = StringVar(value="—")
        ttk.Label(bars, text="Audio/Video", style="Subtle.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.av_progress = ttk.Progressbar(bars, orient="horizontal", mode="determinate", style="Idle.Horizontal.TProgressbar")
        self.av_progress.grid(row=1, column=1, sticky="ew", padx=(12, 12), pady=(8, 0))
        ttk.Label(bars, textvariable=self.av_percent_var, style="Subtle.TLabel").grid(row=1, column=2, sticky="e", pady=(8, 0))

        self.progress_detail_var = StringVar(value="No scan running.")
        ttk.Label(progress_frame, textvariable=self.progress_detail_var, style="Subtle.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))

        log_frame = ttk.LabelFrame(self.content, text="Live log", padding=(16, 12), style="Card.TLabelframe")
        log_frame.grid(row=6, column=0, sticky=(N, S, E, W))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_widget = ScrolledText(log_frame, height=9, width=100, state="disabled", relief="flat", borderwidth=0,
                                       background=self.colors["background"], foreground=self.colors["text"],
                                       insertbackground=self.colors["text"], font=("Consolas", 10))
        self.log_widget.grid(row=0, column=0, sticky=(N, S, E, W))
        self._set_status("Ready.")
        self._update_progress_styles(0, 0)

    def _set_status(self, message: str, level: str = "info"):
        if not hasattr(self, "status_var"):
            self.status_var = StringVar()
        self.status_var.set(message)
        style = self.status_styles.get(level, "StatusInfo.TLabel")
        if hasattr(self, "status_label"):
            self.status_label.configure(style=style)

    def show_banner(self, message: str, level: str = "INFO"):
        self._tool_banner_active = False
        level_key = (level or "INFO").upper()
        styles = self.banner_styles.get(level_key, self.banner_styles["INFO"])
        if self.banner_after_id:
            self.root.after_cancel(self.banner_after_id)
            self.banner_after_id = None
        self.banner_message.set(message)
        self.banner_frame.configure(style=styles["frame"])
        self.banner_label.configure(style=styles["label"])
        if not self._banner_visible:
            self.banner_frame.grid()
            self._banner_visible = True
        if level_key == "SUCCESS":
            self.banner_after_id = self.root.after(8000, self.clear_banner)

    def clear_banner(self):
        if self.banner_after_id:
            self.root.after_cancel(self.banner_after_id)
            self.banner_after_id = None
        if self._banner_visible:
            self.banner_frame.grid_remove()
            self._banner_visible = False
        self.banner_message.set("")
        self._tool_banner_active = False

    def _start_activity_indicator(self):
        if getattr(self, "activity_indicator", None) is None:
            return
        if not self.activity_indicator_running:
            self.activity_indicator.grid()
            self.activity_indicator.start(interval=100)
            self.activity_indicator_running = True

    def _stop_activity_indicator(self):
        if getattr(self, "activity_indicator", None) is None:
            return
        if self.activity_indicator_running:
            try:
                self.activity_indicator.stop()
            except Exception:
                pass
            self.activity_indicator.grid_remove()
            self.activity_indicator_running = False

    def _schedule_status_ticker(self):
        if self.status_line_update_id is None and not self._closing:
            self.status_line_update_id = self.root.after(5000, self._status_ticker_tick)

    def _status_ticker_tick(self):
        self.status_line_update_id = None
        if self._closing:
            return
        self._update_status_line()
        self._schedule_status_ticker()

    def _format_elapsed(self, seconds: Optional[int]) -> str:
        total = max(0, int(seconds or 0))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _update_status_line(self, force: bool = False):
        if not hasattr(self, "status_line_var"):
            return
        now = time.time()
        if self.scan_in_progress:
            if self.last_progress_ts is None:
                self.last_progress_ts = now
            heartbeat_due = (
                self.last_progress_ts is not None
                and now - self.last_progress_ts >= 15
            )
            if heartbeat_due:
                if not self.heartbeat_active:
                    self.heartbeat_active = True
                    self.status_line_label.configure(style="Heartbeat.TLabel")
                self.status_line_var.set(
                    "I am still working — no new items in the last few seconds — please wait"
                )
                return
            if self.heartbeat_active:
                self.heartbeat_active = False
                self.status_line_label.configure(style=self.status_line_active_style)
            snapshot = self.progress_snapshot or {}
            if not snapshot:
                self.status_line_label.configure(style=self.status_line_active_style)
                self.status_line_var.set("Preparing…")
                return
            phase = str(snapshot.get("phase") or "working")
            phase_text = phase.replace("_", " ").replace("-", " ")
            elapsed = snapshot.get("elapsed_s")
            if elapsed is None and self.scan_start_ts:
                elapsed = int(now - self.scan_start_ts)
            files_seen = snapshot.get("files_seen")
            if files_seen is None:
                files_seen = snapshot.get("done_all", 0) or 0
            av_seen = snapshot.get("av_seen")
            if av_seen is None:
                av_seen = snapshot.get("done_av")
            message = (
                f"Working — phase: {phase_text} — elapsed: {self._format_elapsed(elapsed)}"
                f" — files seen: {files_seen:,}"
            )
            if av_seen is not None:
                message += f" — AV files: {av_seen:,}"
            self.status_line_label.configure(style=self.status_line_active_style)
            self.status_line_var.set(message)
        else:
            if self.heartbeat_active:
                self.heartbeat_active = False
            self.status_line_label.configure(style=self.status_line_idle_style)
            self.status_line_var.set(self.status_line_idle_text)

    def _progress_style_for(self, percent: float) -> str:
        if percent >= 100:
            return "Success.Horizontal.TProgressbar"
        if percent >= 75:
            return "Accent.Horizontal.TProgressbar"
        if percent >= 40:
            return "Info.Horizontal.TProgressbar"
        if percent >= 15:
            return "Warning.Horizontal.TProgressbar"
        return "Idle.Horizontal.TProgressbar"

    def _update_progress_styles(self, overall_pct: float, av_pct: float):
        self.total_progress.configure(style=self._progress_style_for(overall_pct))
        self.av_progress.configure(style=self._progress_style_for(av_pct if av_pct else 0))

    def _log_enqueue(self, line: str):
        if line is None:
            return
        line = str(line)
        try:
            self.worker_queue.put_nowait({"type": "log", "line": line})
        except queue.Full:
            pass

    def _append_log(self, line: str):
        if line is None:
            return
        line = str(line)
        self.log_lines.append(line)
        max_keep = 1000
        if len(self.log_lines) > max_keep:
            self.log_lines = self.log_lines[-max_keep:]
        if hasattr(self, "log_widget"):
            self.log_widget.configure(state="normal")
            self.log_widget.delete("1.0", END)
            text = "\n".join(self.log_lines)
            if text:
                self.log_widget.insert(END, text + "\n")
            self.log_widget.see(END)
            self.log_widget.configure(state="disabled")

    def _load_existing_logs(self, max_lines: int = 200):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = [line.rstrip("\n") for line in f.readlines()[-max_lines:]]
            for line in lines:
                self._append_log(line)
        except FileNotFoundError:
            pass
        except Exception as exc:
            self._append_log(f"[log tail error] {exc}")

    # ----- Presets
    def _load_types(self) -> list[str]:
        try:
            with open(TYPES_JSON, "r", encoding="utf-8") as f:
                arr = json.load(f)
                if isinstance(arr, list):
                    return arr
        except Exception:
            pass
        return ["HDD 3.5","HDD 2.5","SSD SATA","NVMe","USB 3.0 enclosure"]

    def _reload_presets(self):
        self.presets = self._load_types()
        self.presets_combo["values"] = self.presets

    def _save_current_type(self):
        t = self.type_var.get().strip()
        if not t: return
        arr = self._load_types()
        if t not in arr:
            arr.append(t)
            with open(TYPES_JSON, "w", encoding="utf-8") as f:
                json.dump(arr, f, ensure_ascii=False, indent=2)
            self._reload_presets()
            messagebox.showinfo("Saved", f"Saved preset: {t}")

    # ----- DB actions
    def _persist_catalog_path(self, path: str):
        try:
            update_settings(WORKING_DIR_PATH, catalog_db=str(Path(path)))
        except Exception as exc:
            log(f"[settings save error] {exc}")

    def db_new(self):
        initial_dir_path = Path(self.db_path.get()).parent if self.db_path.get() else Path(BASE_DIR)
        fn = filedialog.asksaveasfilename(defaultextension=".db", initialdir=str(initial_dir_path), title="Create new catalog DB")
        if not fn: return
        self.db_path.set(fn); init_catalog(fn); self.refresh_all(); self._persist_catalog_path(fn)

    def db_open(self):
        initial_dir_path = Path(self.db_path.get()).parent if self.db_path.get() else Path(BASE_DIR)
        fn = filedialog.askopenfilename(initialdir=str(initial_dir_path), title="Open catalog DB", filetypes=[("SQLite DB","*.db"),("All","*.*")])
        if not fn: return
        self.db_path.set(fn); init_catalog(fn); self.refresh_all(); self._persist_catalog_path(fn)

    def db_reset(self):
        if not messagebox.askyesno("Reset", "This will wipe the current catalog tables (keeps shards). Continue?"):
            return
        init_catalog(self.db_path.get())
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        cur.executescript("DELETE FROM jobs; DELETE FROM drives; VACUUM;")
        con.commit(); con.close()
        self.refresh_all()

    def db_vacuum(self):
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE);"); con.commit()
        cur.execute("VACUUM;"); con.commit(); con.close()
        messagebox.showinfo("VACUUM", "Done.")

    # ----- Buttons
    def choose_path(self):
        d = filedialog.askdirectory()
        if d: self.path_var.set(d)

    def export_db(self):
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dst = Path(EXPORTS_DIR) / f"catalog_{ts}.db"
        shutil.copy2(self.db_path.get(), dst)
        messagebox.showinfo("Export", f"Exported to:\n{dst}")

    def del_drive(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select", "Select a drive row first.")
            return
        row = self.tree.item(sel[0])["values"]; label = row[1]
        if not messagebox.askyesno("Confirm", f"Delete drive '{label}' from catalog only?"):
            return
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        cur.execute("DELETE FROM drives WHERE label=?", (label,))
        con.commit(); con.close()
        self.refresh_all()

    def del_job(self):
        sel = self.jobs.selection()
        if not sel:
            messagebox.showinfo("Select", "Select a job row first.")
            return
        jid = self.jobs.item(sel[0])["values"][0]
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        cur.execute("DELETE FROM jobs WHERE id=?", (jid,))
        con.commit(); con.close()
        self.refresh_all()

    def resume_job(self):
        sel = self.jobs.selection()
        if not sel:
            messagebox.showinfo("Resume", "Select a job row to resume.")
            return
        values = self.jobs.item(sel[0]).get("values", [])
        if len(values) < 3:
            messagebox.showwarning("Resume", "Unable to read the selected job.")
            return
        drive_label = values[1]
        job_status = str(values[2]) if len(values) > 2 else ""
        if job_status and job_status.lower() not in {"stopped", "done"}:
            if not messagebox.askyesno("Resume job", f"Job is marked as '{job_status}'. Resume anyway?"):
                return
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        cur.execute("SELECT mount_path, COALESCE(drive_type,''), COALESCE(notes,'') FROM drives WHERE label=?", (drive_label,))
        row = cur.fetchone()
        con.close()
        if not row:
            messagebox.showwarning("Resume job", f"Drive '{drive_label}' not found in catalog.")
            return
        mount_path, dtype, notes = row
        self.label_var.set(drive_label)
        self.path_var.set(mount_path)
        self.type_var.set(dtype)
        self.notes_var.set(notes)
        if not messagebox.askyesno("Resume job", f"Restart scanning drive '{drive_label}'?\nMount: {mount_path}"):
            return
        self._set_status(f"Resuming {drive_label}…", "accent")
        tool_state = self._ensure_tools_before_scan()
        if tool_state is None:
            return
        self._launch_worker(tool_state)

    def update_drive(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select drive","Please select a drive row first.")
            return
        item = self.tree.item(sel[0])["values"]
        drive_id, old_label = item[0], item[1]
        new_label = self.label_var.get().strip() or old_label
        new_mount = self.path_var.get().strip() or item[2]
        new_type  = self.type_var.get().strip() or item[3]
        new_notes = self.notes_var.get().strip() or item[4]
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        if new_label != old_label:
            old = shard_path_for(old_label)
            new = shard_path_for(new_label)
            if old.exists():
                try: old.replace(new)
                except Exception as e:
                    messagebox.showwarning("Shard move", f"Could not rename shard: {e}")
        cur.execute("UPDATE drives SET label=?, mount_path=?, drive_type=?, notes=? WHERE id=?",
                    (new_label, new_mount, new_type, new_notes, drive_id))
        con.commit(); con.close()
        self.refresh_all(); self._set_status(f"Updated drive {new_label}.", "success")

    def open_shard_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select drive","Select a drive row first."); return
        label = self.tree.item(sel[0])["values"][1]
        sp = str(shard_path_for(label))
        if not os.path.exists(sp):
            messagebox.showinfo("Shard", f"No shard for {label}."); return
        try:
            if has_cmd("sqlite3"):
                subprocess.Popen(["cmd","/c",f'start cmd /k sqlite3 "{sp}"'], shell=True)
            else:
                os.startfile(Path(sp).parent)
        except Exception as e:
            messagebox.showwarning("Open shard", str(e))

    def start_scan(self):
        label = self.label_var.get().strip()
        mount = self.path_var.get().strip()
        if not (label and mount):
            messagebox.showerror("Missing", "Please fill Mount Path and Disk Label."); return
        tool_state = self._ensure_tools_before_scan()
        if tool_state is None:
            return
        self._launch_worker(tool_state)

    def rescan(self):
        label = self.label_var.get().strip()
        mount = self.path_var.get().strip()
        if not (label and mount):
            messagebox.showerror("Missing", "Please fill Mount Path and Disk Label."); return
        tool_state = self._ensure_tools_before_scan()
        if tool_state is None:
            return
        shard_path = shard_path_for(label)
        for _ in range(5):
            try:
                if shard_path.exists(): os.remove(shard_path)
                break
            except PermissionError: time.sleep(0.3)
        self._launch_worker(tool_state)

    def _launch_worker(self, tool_state: Dict[str, dict]):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A scan is already running."); return
        self.stop_evt = threading.Event()
        self._clear_worker_queue()
        try:
            threads = int(self.threads_var.get())
        except (TypeError, ValueError):
            threads = 1
        threads = max(1, min(32, threads))
        self.worker = ScannerWorker(
            label=self.label_var.get().strip(),
            mount=self.path_var.get().strip(),
            notes=self.notes_var.get().strip(),
            dtype=self.type_var.get().strip(),
            db_catalog=self.db_path.get(),
            blake_for_av=bool(self.blake_var.get()),
            max_workers=threads,
            stop_evt=self.stop_evt,
            event_queue=self.worker_queue,
            tool_state=tool_state,
        )
        self.clear_banner()
        self.show_banner("Scan started", "INFO")
        self._set_status("Scanning…", "accent")
        self.progress_percent_var.set("0%")
        self.total_percent_var.set("0%")
        self.av_percent_var.set("0%")
        self.progress_detail_var.set("Preparing scan…")
        self.total_progress["value"] = 0
        self.total_progress["maximum"] = 1
        self.av_progress["value"] = 0
        self.av_progress["maximum"] = 1
        self._update_progress_styles(0, 0)
        self.scan_in_progress = True
        self.scan_start_ts = time.time()
        self.last_progress_ts = self.scan_start_ts
        self.progress_snapshot = {
            "phase": "enumerating",
            "files_total": None,
            "files_seen": 0,
            "av_seen": 0,
            "elapsed_s": 0,
            "total_av": None,
        }
        self.heartbeat_active = False
        self.status_line_idle_text = "Ready."
        self._start_activity_indicator()
        self._update_status_line(force=True)
        self.worker.start()
        self.root.after(800, self.refresh_jobs)

    def stop_scan(self):
        if self.stop_evt:
            self.stop_evt.set()
        self._set_status("Stopping…", "warning")

    # ----- Progress & refresh
    def _clear_worker_queue(self):
        try:
            while True:
                self.worker_queue.get_nowait()
        except queue.Empty:
            pass

    def _poll_worker_queue(self):
        try:
            while True:
                event = self.worker_queue.get_nowait()
                self._handle_worker_event(event)
        except queue.Empty:
            pass
        finally:
            if not self._closing:
                self.root.after(200, self._poll_worker_queue)

    def _handle_worker_event(self, event: dict):
        etype = event.get("type")
        if etype == "progress":
            files_total = event.get("files_total")
            if files_total is None:
                alt_total = event.get("total_all")
                if alt_total is not None:
                    files_total = alt_total
            files_seen = event.get("files_seen")
            if files_seen is None:
                files_seen = event.get("done_all", 0) or 0
            av_seen = event.get("av_seen")
            if av_seen is None:
                av_seen = event.get("done_av")
            total_av = event.get("total_av")
            if total_av is None and self.progress_snapshot:
                total_av = self.progress_snapshot.get("total_av")
            phase = event.get("phase") or (self.progress_snapshot or {}).get("phase") or "working"
            elapsed = event.get("elapsed_s")

            self.total_progress["maximum"] = max(1, files_total or 1)
            self.total_progress["value"] = files_seen
            av_total_for_bar = total_av if total_av not in (None, 0) else (total_av or 1)
            self.av_progress["maximum"] = max(1, av_total_for_bar)
            self.av_progress["value"] = av_seen or 0

            overall_pct = (files_seen / files_total * 100) if files_total else 0.0
            av_pct = (av_seen / total_av * 100) if (total_av not in (None, 0)) else 0.0

            self.progress_percent_var.set(f"{overall_pct:.0f}%")
            self.total_percent_var.set(f"{overall_pct:.0f}%")
            self.av_percent_var.set(
                f"{av_pct:.0f}%" if total_av not in (None, 0) else "—"
            )

            if files_total:
                if total_av not in (None, 0):
                    detail = f"{files_seen:,}/{files_total:,} files · {av_seen or 0:,}/{total_av:,} AV"
                else:
                    detail = f"{files_seen:,}/{files_total:,} files"
                status_text = f"Scanning • {files_seen:,}/{files_total:,} files"
            else:
                detail = "Gathering files…"
                status_text = "Scanning…"
            self.progress_detail_var.set(detail)
            self._set_status(status_text, "accent")
            self._update_progress_styles(overall_pct, av_pct if total_av not in (None, 0) else 0.0)

            self.progress_snapshot = {
                "phase": phase,
                "files_total": files_total,
                "files_seen": files_seen,
                "av_seen": av_seen if av_seen is not None else 0,
                "elapsed_s": elapsed,
                "total_av": total_av,
                "done_av": av_seen if av_seen is not None else 0,
                "done_all": files_seen,
            }
            if event.get("total_all") is not None:
                self.progress_snapshot["total_all"] = event.get("total_all")
            self.last_progress_ts = time.time()
            if self.heartbeat_active:
                self.heartbeat_active = False
                self.status_line_label.configure(style=self.status_line_active_style)
            self._update_status_line(force=True)
        elif etype == "status":
            msg = event.get("message", "")
            lower = msg.lower()
            if "complete" in lower or "ready" in lower:
                level = "success"
            elif "stop" in lower or "error" in lower:
                level = "warning"
            else:
                level = "info"
            self._set_status(msg, level)
        elif etype == "tool_missing":
            tool = str(event.get("tool", "")).strip() or "tool"
            self.scan_in_progress = False
            self._stop_activity_indicator()
            message = f"Error — {tool} not found. Please install {tool} and try again."
            self.show_banner(message, "ERROR")
            self._set_status("Scan error.", "error")
            self.progress_detail_var.set("Scan stopped due to missing tool.")
            self.status_line_idle_text = message
            self.progress_snapshot = None
            self.last_progress_ts = None
            self._update_status_line(force=True)
        elif etype == "refresh_jobs":
            self.refresh_jobs()
        elif etype == "error":
            messagebox.showerror(event.get("title", "Error"), event.get("message", ""))
        elif etype == "log":
            self._append_log(event.get("line", ""))
        elif etype == "done":
            self._await_worker_completion()
            status = event.get("status", "Ready")
            self.scan_in_progress = False
            self._stop_activity_indicator()
            self.heartbeat_active = False
            self.last_progress_ts = None
            self.scan_start_ts = None
            summary_snapshot = self.progress_snapshot or {}
            total_all = event.get("total_all")
            if total_all is None:
                total_all = summary_snapshot.get("files_total")
            total_av = event.get("done_av")
            if total_av is None:
                total_av = summary_snapshot.get("av_seen")
            duration = event.get("duration")
            if duration is None and summary_snapshot.get("elapsed_s") is not None:
                duration = summary_snapshot.get("elapsed_s")
            if status == "Done":
                self._set_status("Ready.", "success")
                summary_text = (
                    "Done — total files: "
                    f"{(total_all or 0):,} — AV files: {(total_av or 0):,} — duration: {self._format_elapsed(duration)}"
                )
                self.status_line_idle_text = summary_text
                self.show_banner("Scan completed successfully", "SUCCESS")
            elif status == "Stopped":
                self._set_status("Scan stopped.", "warning")
                self.status_line_idle_text = "Scan canceled."
                self.show_banner("Scan canceled", "WARNING")
            else:
                default_msg = event.get("message") or status or "Scan error."
                if status.lower() == "error":
                    self.show_banner(default_msg, "ERROR")
                elif not self._banner_visible:
                    self.show_banner(default_msg, "WARNING")
                self._set_status(default_msg)
                self.status_line_idle_text = default_msg
            self.progress_detail_var.set("No scan running.")
            self.progress_percent_var.set("0%")
            self.total_percent_var.set("0%")
            self.av_percent_var.set("—")
            self.total_progress["value"] = 0
            self.total_progress["maximum"] = 1
            self.av_progress["value"] = 0
            self.av_progress["maximum"] = 1
            self._update_progress_styles(0, 0)
            self.progress_snapshot = None
            self._update_status_line(force=True)

    def _try_finalize_worker(self):
        if self.worker and not self.worker.is_alive():
            self.worker.join()
            self.worker = None
            self.stop_evt = None

    def refresh_all(self):
        self.refresh_drives(); self.refresh_jobs()

    def refresh_drives(self):
        self.tree.delete(*self.tree.get_children())
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        for row in cur.execute("""
            SELECT id,label,mount_path,COALESCE(drive_type,''),COALESCE(notes,''),COALESCE(serial,''),COALESCE(model,''),
                   ROUND(COALESCE(total_bytes,0)/1024.0/1024/1024,2)
            FROM drives ORDER BY id ASC
        """):
            self.tree.insert("", END, values=row)
        con.close()

    def refresh_jobs(self):
        self.jobs.delete(*self.jobs.get_children())
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        for row in cur.execute("""
            SELECT id,drive_label,status,COALESCE(done_av,0),COALESCE(total_av,0),COALESCE(total_all,0),
                   started_at,COALESCE(finished_at,''),COALESCE(duration_sec,0),COALESCE(message,'')
            FROM jobs ORDER BY id DESC LIMIT 200
        """):
            self.jobs.insert("", END, values=row)
        con.close()
        if self.worker and self.worker.is_alive():
            self.root.after(1200, self.refresh_jobs)
        elif not (self.worker and self.worker.is_alive()):
            if self.worker is None:
                self._set_status("Ready.")

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        unregister_log_listener(self._log_enqueue)
        if self.worker and self.worker.is_alive():
            if self.stop_evt:
                self.stop_evt.set()
            self._set_status("Stopping…", "warning")
            self._await_worker_completion(self.root.destroy)
        else:
            self.root.destroy()

    def _await_worker_completion(self, on_complete=None):
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=0.1)
            self.root.after(100, lambda: self._await_worker_completion(on_complete))
        else:
            self._try_finalize_worker()
            if on_complete:
                on_complete()


class DiagnosticsDialog(Toplevel):
    def __init__(self, app: App):
        super().__init__(app.root)
        self.app = app
        self.title("Tool Diagnostics")
        self.transient(app.root)
        self.grab_set()
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.rows: Dict[ToolName, dict] = {}

        container = ttk.Frame(self, padding=(20, 18), style="Content.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        headings = ("Tool", "Status", "Version", "Path", "Actions")
        for idx, text in enumerate(headings):
            ttk.Label(container, text=text, style="TreeHeading.TLabel").grid(
                row=0, column=idx, sticky="w", padx=(0, 8)
            )

        for index, tool in enumerate(ALL_TOOLS):
            display = TOOL_DISPLAY[tool]
            row_base = index * 2 + 1
            ttk.Label(container, text=display, style="Subtle.TLabel").grid(
                row=row_base, column=0, sticky="w"
            )
            status_label = ttk.Label(container, text="—", style="StatusWarning.TLabel")
            status_label.grid(row=row_base, column=1, sticky="w", padx=(0, 8))
            version_var = StringVar(value="—")
            ttk.Label(container, textvariable=version_var, style="Subtle.TLabel").grid(
                row=row_base, column=2, sticky="w"
            )
            path_var = StringVar(value="—")
            ttk.Label(container, textvariable=path_var, style="Subtle.TLabel").grid(
                row=row_base, column=3, sticky="w"
            )

            actions = ttk.Frame(container, style="Card.TFrame")
            actions.grid(row=row_base, column=4, sticky="e", padx=(0, 4))
            ttk.Button(actions, text="Install…", command=lambda t=tool: self._install(t)).grid(
                row=0, column=0, padx=(0, 4)
            )
            ttk.Button(actions, text="Locate…", command=lambda t=tool: self._locate(t)).grid(
                row=0, column=1, padx=(0, 4)
            )
            ttk.Button(actions, text="Recheck", command=lambda t=tool: self._recheck(t)).grid(
                row=0, column=2
            )

            error_var = StringVar(value="")
            error_label = ttk.Label(
                container,
                textvariable=error_var,
                style="Subtle.TLabel",
                wraplength=520,
            )
            error_label.grid(row=row_base + 1, column=0, columnspan=5, sticky="w", pady=(2, 8))

            self.rows[tool] = {
                "status_label": status_label,
                "version_var": version_var,
                "path_var": path_var,
                "error_var": error_var,
            }

        button_row = ttk.Frame(container, style="Card.TFrame")
        button_row.grid(
            row=len(ALL_TOOLS) * 2 + 1,
            column=0,
            columnspan=5,
            sticky="e",
            pady=(12, 0),
        )
        ttk.Button(button_row, text="Close", command=self._on_close).grid(row=0, column=0, sticky="e")

        self.update_rows(self.app.tool_status)

    def update_rows(self, statuses: Dict[str, dict]):
        for tool, widgets in self.rows.items():
            info = statuses.get(tool) or {}
            present = bool(info.get("present"))
            status_text = "Present" if present else "Missing"
            style = "StatusSuccess.TLabel" if present else "StatusDanger.TLabel"
            widgets["status_label"].configure(text=status_text, style=style)
            widgets["version_var"].set(info.get("version") or "—")
            widgets["path_var"].set(info.get("path") or "—")
            errors = info.get("errors") or []
            widgets["error_var"].set("; ".join(errors) if errors else "")

    def _install(self, tool: ToolName):
        dialog = self.app.open_install_dialog(tool)
        if dialog is not None:
            self.wait_window(dialog)
        self.update_rows(self.app.tool_status)

    def _locate(self, tool: ToolName):
        self.app.locate_tool_manually(tool)

    def _recheck(self, tool: ToolName):
        self.app.recheck_tool(tool)
        self.update_rows(self.app.tool_status)

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        if self in self.app._diagnostics_windows:
            self.app._diagnostics_windows.remove(self)
        self.destroy()


class ToolInstallDialog(Toplevel):
    def __init__(self, app: App, tool: ToolName):
        super().__init__(app.root)
        self.app = app
        self.tool = tool
        self.title(f"Install {TOOL_DISPLAY[tool]}")
        self.transient(app.root)
        self.grab_set()
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.install_thread: Optional[threading.Thread] = None
        self.cancel_event: Optional[threading.Event] = None

        container = ttk.Frame(self, padding=(20, 18), style="Content.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        winget_text = ", ".join(get_winget_candidates(tool))
        instructions = (
            f"Install {TOOL_DISPLAY[tool]} via winget or provide a portable folder. "
            f"winget IDs: {winget_text}."
        )
        ttk.Label(container, text=instructions, style="Subtle.TLabel", wraplength=520).grid(
            row=0, column=0, sticky="w"
        )

        self.status_var = StringVar(value="Ready.")
        ttk.Label(container, textvariable=self.status_var, style="Subtle.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 4)
        )

        self.progress = ttk.Progressbar(
            container,
            orient="horizontal",
            mode="indeterminate",
            length=320,
            style="Info.Horizontal.TProgressbar",
        )
        self.progress.grid(row=2, column=0, sticky="ew", pady=(4, 8))
        self.progress.grid_remove()

        self.log_widget = ScrolledText(
            container,
            height=10,
            width=70,
            state="disabled",
            relief="flat",
            borderwidth=0,
            background=self.app.colors["background"],
            foreground=self.app.colors["text"],
        )
        self.log_widget.grid(row=3, column=0, sticky="nsew", pady=(0, 12))
        container.rowconfigure(3, weight=1)

        buttons = ttk.Frame(container, style="Card.TFrame")
        buttons.grid(row=4, column=0, sticky="e")

        self.install_button = ttk.Button(
            buttons,
            text="Install with winget",
            command=self.start_winget_install,
        )
        self.install_button.grid(row=0, column=0, padx=(0, 8))

        self.portable_button = ttk.Button(
            buttons,
            text="Portable setup…",
            command=self.choose_portable_folder,
        )
        self.portable_button.grid(row=0, column=1, padx=(0, 8))

        self.cancel_button = ttk.Button(
            buttons,
            text="Cancel install",
            command=self.cancel_install,
            style="Danger.TButton",
        )
        self.cancel_button.grid(row=0, column=2, padx=(0, 8))
        self.cancel_button.grid_remove()

        ttk.Button(buttons, text="Close", command=self._on_close).grid(row=0, column=3)

    def _append_log(self, line: str):
        self.log_widget.configure(state="normal")
        self.log_widget.insert(END, line + "\n")
        self.log_widget.see(END)
        self.log_widget.configure(state="disabled")

    def _set_install_running(self, running: bool):
        if running:
            self.progress.grid()
            self.progress.start(100)
            self.install_button.configure(state="disabled")
            self.portable_button.configure(state="disabled")
            self.cancel_button.grid()
            self.status_var.set("Installing via winget…")
        else:
            try:
                self.progress.stop()
            except Exception:
                pass
            self.progress.grid_remove()
            self.install_button.configure(state="normal")
            self.portable_button.configure(state="normal")
            self.cancel_button.grid_remove()
            if self.install_thread and self.install_thread.is_alive():
                self.status_var.set("Cancelling…")
            else:
                self.status_var.set("Ready.")

    def start_winget_install(self):
        if self.install_thread and self.install_thread.is_alive():
            return
        if not winget_available():
            messagebox.showerror("winget unavailable", "winget is not available. Try the portable setup option.")
            return
        self.cancel_event = threading.Event()

        def worker():
            success, message, cancelled = install_tool_via_winget(
                self.tool,
                cancel_event=self.cancel_event,
                output_callback=lambda line: self.after(0, lambda: self._append_log(line)),
            )
            self.after(0, lambda: self._winget_finished(success, message, cancelled))

        self._append_log("Starting winget installation…")
        self._set_install_running(True)
        self.install_thread = threading.Thread(target=worker, daemon=True)
        self.install_thread.start()

    def _winget_finished(self, success: bool, message: str, cancelled: bool):
        self._set_install_running(False)
        self.install_thread = None
        self.cancel_event = None
        if cancelled:
            self.status_var.set("Installation cancelled.")
            self._append_log("Installation cancelled by user.")
            return
        if success:
            display = TOOL_DISPLAY[self.tool]
            log(f"Installed {self.tool} via winget.")
            self._append_log(message)
            self.status_var.set("Installation completed.")
            messagebox.showinfo("Install", f"Installed {display} via winget.")
            self.app.refresh_tool_statuses()
        else:
            self.status_var.set("Installation failed.")
            self._append_log(message)
            messagebox.showerror("Install", message)

    def cancel_install(self):
        if self.cancel_event and not self.cancel_event.is_set():
            self.cancel_event.set()
            self._append_log("Cancelling winget installation…")

    def choose_portable_folder(self):
        folder = filedialog.askdirectory(title=f"Select portable folder for {TOOL_DISPLAY[self.tool]}")
        if not folder:
            return
        success, message, _ = setup_portable_tool(self.tool, folder, WORKING_DIR_PATH)
        if success:
            self._append_log(message)
            log(message)
            messagebox.showinfo("Portable setup", message)
            self.app.refresh_tool_statuses()
        else:
            self._append_log(message)
            messagebox.showerror("Portable setup", message)

    def _on_close(self):
        if self.install_thread and self.install_thread.is_alive():
            messagebox.showwarning("Installation running", "Cancel the installation before closing this window.")
            return
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

# ---------------- main ----------------
def main():
    ensure_working_dir_structure(WORKING_DIR_PATH)
    init_catalog(DB_DEFAULT)
    root = Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
