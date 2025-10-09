# DiskScannerGUI.py — V8.2 (Yann)
# Requiert: Python 3.10+, mediainfo (CLI), sqlite3 (intégré), smartctl/ffmpeg optionnels, blake3 (pip)
# Dossier base: C:\Users\Administrator\VideoCatalog

import os, sys, json, time, shutil, sqlite3, threading, subprocess, zipfile, queue, webbrowser, base64
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
from types import SimpleNamespace

import scan_drive
from structure import load_structure_settings as load_structure_config

from db_maint import (
    MaintenanceOptions,
    check_integrity,
    database_size_bytes,
    light_backup,
    quick_optimize,
    reindex_and_analyze,
    resolve_options,
    update_maintenance_metadata,
    vacuum_if_needed,
)

from core.paths import (
    ensure_working_dir_structure,
    get_catalog_db_path,
    get_drive_types_path,
    get_exports_dir,
    get_logs_dir,
    get_scans_dir,
    get_shard_db_path,
    get_shards_dir,
    resolve_working_dir,
    safe_label,
)
from core.settings import load_settings, update_settings
from exports import ExportFilters, export_shard
from perf import resolve_performance_config
from tools import (
    bootstrap_local_bin,
    get_winget_candidates,
    install_tool_via_winget,
    probe_tool,
    set_manual_tool_path,
    setup_portable_tool,
    winget_available,
)
from search_util import (
    sanitize_query,
    ensure_inventory_name,
    build_search_query,
    format_results,
    export_results,
)
from core.search_plus_client import SearchPlusClient
import reports_util
from tkinter import (
    Tk, Toplevel, StringVar, IntVar, END, N, S, E, W,
    filedialog, messagebox, ttk, Menu, Spinbox, PhotoImage
)
from tkinter.scrolledtext import ScrolledText

from gpu.capabilities import probe_gpu
from gpu.runtime import get_hwaccel_args, select_provider

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

_api_settings = _SETTINGS.get("api") if isinstance(_SETTINGS, dict) else {}
if isinstance(_api_settings, dict):
    API_ENABLED_DEFAULT = bool(_api_settings.get("enabled_default", False))
    API_HOST_DEFAULT = str(_api_settings.get("host") or "127.0.0.1")
    try:
        API_PORT_DEFAULT = int(_api_settings.get("port") or 8756)
    except (TypeError, ValueError):
        API_PORT_DEFAULT = 8756
    API_KEY_PRESENT_DEFAULT = bool(str(_api_settings.get("api_key") or "").strip())
    API_KEY_VALUE_DEFAULT = str(_api_settings.get("api_key") or "").strip()
else:
    API_ENABLED_DEFAULT = False
    API_HOST_DEFAULT = "127.0.0.1"
    API_PORT_DEFAULT = 8756
    API_KEY_PRESENT_DEFAULT = False
    API_KEY_VALUE_DEFAULT = ""

API_SCRIPT_PATH = Path(__file__).resolve().with_name("videocatalog_api.py")

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


def format_bytes(num: int) -> str:
    if num <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


class SimpleTooltip:
    def __init__(self, widget):
        self.widget = widget
        self.text: str = ""
        self.tipwindow: Optional[Toplevel] = None
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._on_leave)

    def set_text(self, text: str) -> None:
        self.text = text or ""
        if self.tipwindow and not self.text:
            self._hide()

    def _on_enter(self, _event=None):
        if self.text:
            self._show()

    def _on_leave(self, _event=None):
        self._hide()

    def _show(self):
        if self.tipwindow or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except Exception:
            return
        self.tipwindow = tw = Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        frame = ttk.Frame(tw, padding=(6, 4), style="Card.TFrame")
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text=self.text, justify="left", style="Subtle.TLabel", wraplength=320).grid(
            row=0, column=0, sticky="w"
        )

    def _hide(self):
        if self.tipwindow is not None:
            try:
                self.tipwindow.destroy()
            except Exception:
                pass
            self.tipwindow = None

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
          mtime_utc TEXT,
          deleted INTEGER DEFAULT 0,
          deleted_ts TEXT
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
        ("deleted", "INTEGER DEFAULT 0"),
        ("deleted_ts", "TEXT"),
    ]
    cur.execute("PRAGMA table_info(files)")
    existing_columns = {row[1] for row in cur.fetchall()}
    for col_name, col_type in required_columns:
        if col_name not in existing_columns:
            cur.execute(f"ALTER TABLE files ADD COLUMN {col_name} {col_type}")

    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_files_path ON files(path);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash_blake3);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_files_deleted ON files(deleted);")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_state(
          key TEXT PRIMARY KEY,
          value TEXT
        )
        """
    )

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


@dataclass(slots=True)
class FileInfo:
    path: str
    size_bytes: int
    mtime_utc: str
    is_av: bool


@dataclass(slots=True)
class MaintenanceTarget:
    identifier: str
    name: str
    path: Path
    kind: Literal["catalog", "shard"]
    size_bytes: int
    label: str
    safe_label: str


class ShardScanState:
    def __init__(self, conn: sqlite3.Connection, interval_seconds: int = 5):
        self.conn = conn
        self.interval_seconds = max(1, int(interval_seconds))
        self._last_checkpoint = 0.0

    def load(self) -> Dict[str, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT key, value FROM scan_state")
        return {key: value for key, value in cur.fetchall()}

    def clear(self) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM scan_state")
        self.conn.commit()
        self._last_checkpoint = 0.0

    def checkpoint(self, phase: str, last_path: Optional[str], *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_checkpoint) < self.interval_seconds:
            return
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            ("phase", phase),
            ("timestamp", timestamp),
        ]
        if last_path is not None:
            rows.append(("last_path_processed", last_path))
        cur = self.conn.cursor()
        cur.executemany(
            "INSERT INTO scan_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            rows,
        )
        self.conn.commit()
        self._last_checkpoint = now

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
        light_analysis: bool,
        inventory_only: bool,
        gpu_policy: Optional[str],
        gpu_hwaccel: Optional[bool],
        max_workers: int,
        full_rescan: bool,
        resume_enabled: bool,
        checkpoint_seconds: int,
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
        self.light_analysis = bool(light_analysis)
        self.inventory_only = bool(inventory_only)
        self.gpu_policy = (gpu_policy or "").upper() if gpu_policy else None
        self.gpu_hwaccel = gpu_hwaccel
        self.max_workers = max(1, int(max_workers))
        self.full_rescan = bool(full_rescan)
        self.resume_enabled = bool(resume_enabled)
        self.checkpoint_seconds = max(1, int(checkpoint_seconds))
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
        self._gpu_overrides: Dict[str, object] = {}
        if self.gpu_policy:
            self._gpu_overrides["policy"] = self.gpu_policy
        if self.gpu_hwaccel is not None:
            self._gpu_overrides["allow_hwaccel_video"] = bool(self.gpu_hwaccel)

    def _put(self, payload: dict):
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            pass

    def _inventory_progress_callback(self, payload: dict) -> None:
        if self.stop_evt.is_set():
            return
        payload = dict(payload)
        self._current_phase = payload.get("phase", self._current_phase)
        self._put(payload)

    def _run_inventory_only(self, mount: Path, shard_path: Path) -> None:
        self._current_phase = "enumerating"
        self._put({"type": "status", "message": "Inventory Only — enumerating files…"})
        settings_data = load_settings(WORKING_DIR_PATH)
        perf_overrides: Dict[str, object] = {}
        if self.max_workers:
            perf_overrides["worker_threads"] = self.max_workers
        robust_overrides: Dict[str, object] = {}
        try:
            result = scan_drive.scan_drive(
                self.label,
                str(mount),
                self.db_catalog,
                shard_db_path=str(shard_path),
                inventory_only=True,
                resume=True,
                checkpoint_seconds=self.checkpoint_seconds,
                debug_slow=False,
                settings=settings_data,
                perf_overrides=perf_overrides,
                robust_overrides=robust_overrides,
                light_analysis=False,
                gpu_overrides=self._gpu_overrides,
                progress_callback=self._inventory_progress_callback,
            )
        except SystemExit as exc:
            message = f"Inventory failed ({exc.code})."
            self._put({"type": "error", "title": "Inventory error", "message": message})
            self._put({"type": "done", "status": "Error", "message": message})
            return
        except Exception as exc:
            message = f"Inventory error: {exc}"
            self._put({"type": "error", "title": "Inventory error", "message": message})
            self._put({"type": "done", "status": "Error", "message": message})
            return

        totals: Dict[str, int] = {}
        if isinstance(result, dict):
            maybe_totals = result.get("totals")
            if isinstance(maybe_totals, dict):
                totals = {k: int(v or 0) for k, v in maybe_totals.items()}
        total_files = int(result.get("total_files", 0)) if isinstance(result, dict) else 0
        duration_seconds = float(result.get("duration_seconds", 0.0)) if isinstance(result, dict) else 0.0
        total_seconds = int(round(duration_seconds))
        hours, remainder = divmod(max(0, total_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_text = f"{hours:02}:{minutes:02}:{seconds:02}"
        summary_line = (
            "Inventory done — total files: "
            f"{total_files:,} — video: {totals.get('video', 0):,} — "
            f"audio: {totals.get('audio', 0):,} — image: {totals.get('image', 0):,} "
            f"— duration: {duration_text}"
        )
        self._put({"type": "status", "message": summary_line})
        self._put(
            {
                "type": "done",
                "status": "Done",
                "total_all": total_files,
                "done_av": 0,
                "total_av": 0,
                "duration": int(round(duration_seconds)),
                "message": summary_line,
                "inventory_totals": totals,
                "skipped_perm": int(result.get("skipped_perm", 0)) if isinstance(result, dict) else 0,
                "skipped_toolong": int(result.get("skipped_toolong", 0)) if isinstance(result, dict) else 0,
                "skipped_ignored": int(result.get("skipped_ignored", 0)) if isinstance(result, dict) else 0,
            }
        )

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

        if self.inventory_only:
            self._run_inventory_only(mount, shard_path)
            return

        settings_data = load_settings(WORKING_DIR_PATH)
        perf_payload = None
        perf_cfg = None
        try:
            overrides: Dict[str, object] = {}
            if self.max_workers:
                overrides["worker_threads"] = self.max_workers
            perf_cfg = resolve_performance_config(
                str(mount),
                settings=settings_data,
                cli_overrides=overrides,
            )
            perf_payload = {
                "type": "performance",
                "profile": perf_cfg.profile,
                "auto_profile": perf_cfg.auto_profile,
                "source": perf_cfg.source,
                "worker_threads": perf_cfg.worker_threads,
                "hash_chunk_bytes": perf_cfg.hash_chunk_bytes,
                "ffmpeg_parallel": perf_cfg.ffmpeg_parallel,
                "gentle_io": bool(perf_cfg.gentle_io),
            }
        except Exception as exc:
            log(f"[ScannerWorker] performance detection failed: {exc}")
            perf_payload = None
        if perf_payload:
            self._put(perf_payload)

        self._put({"type": "status", "message": "Enumerating files…"})
        self._current_phase = "enumerating"

        def _iso(ts: float) -> str:
            return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")

        file_infos: list[FileInfo] = []
        av_count = 0
        stack: list[Path] = [mount]
        last_emit = time.perf_counter()
        while stack and not self.stop_evt.is_set():
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if self.stop_evt.is_set():
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        try:
                            stat = entry.stat(follow_symlinks=False)
                        except (FileNotFoundError, PermissionError, OSError):
                            continue
                        info = FileInfo(
                            path=entry.path,
                            size_bytes=int(stat.st_size),
                            mtime_utc=_iso(stat.st_mtime),
                            is_av=Path(entry.path).suffix.lower() in AV_EXTS,
                        )
                        file_infos.append(info)
                        if info.is_av:
                            av_count += 1
                        now = time.perf_counter()
                        if (now - last_emit) >= 5:
                            self._emit_progress(self._current_phase, len(file_infos), av_count)
                            last_emit = now
            except PermissionError:
                log(f"[Scan {self.label}] Permission denied while listing {current}")
            except FileNotFoundError:
                continue

        file_infos.sort(key=lambda item: item.path)
        total_all = len(file_infos)
        total_av = av_count
        self._total_all = total_all
        self._total_av = total_av
        self._emit_progress(self._current_phase, total_all, total_av, total_all=total_all, force=True)

        if self.stop_evt.is_set():
            self._put({"type": "status", "message": "Scan stopped."})
            self._put({"type": "done", "status": "Stopped"})
            return

        started_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        smart_blob = try_smart_overview(self.tool_paths.get("smartctl"))
        total_bytes, free_bytes = disk_usage_bytes(mount)

        light_cfg = scan_drive._resolve_light_analysis(settings_data, self.light_analysis)
        gpu_cfg = scan_drive._resolve_gpu_settings(settings_data, self._gpu_overrides)
        light_pipeline: Optional[scan_drive.LightAnalysisPipeline] = None
        light_summary: Optional[Dict[str, object]] = None

        init_shard(str(shard_path))
        with sqlite3.connect(str(shard_path)) as shard:
            _ensure_shard_schema(shard)
            if light_cfg.enabled:
                perf_profile = str(perf_cfg.profile) if perf_cfg else "AUTO"
                light_pipeline = scan_drive.LightAnalysisPipeline(
                    settings=light_cfg,
                    gpu_settings=gpu_cfg,
                    connection=shard,
                    ffmpeg_path=self.tool_paths.get("ffmpeg"),
                    perf_profile=perf_profile,
                    progress_callback=self._put,
                    start_time=self._start_monotonic or time.perf_counter(),
                )
                light_pipeline.prepare()
            state_store = ShardScanState(shard, interval_seconds=self.checkpoint_seconds)
            if not self.resume_enabled:
                state_store.clear()
                resume_state: Dict[str, str] = {}
            else:
                resume_state = state_store.load()

            scur = shard.cursor()
            scur.execute("SELECT path, size_bytes, is_av, integrity_ok, mtime_utc, deleted FROM files")
            existing: Dict[str, dict] = {}
            for path, size_bytes, is_av, integrity_ok, mtime_utc, deleted in scur.fetchall():
                existing[path] = {
                    "size_bytes": size_bytes,
                    "is_av": int(is_av or 0),
                    "integrity_ok": integrity_ok,
                    "mtime_utc": mtime_utc,
                    "deleted": int(deleted or 0),
                }

            existing_paths = set(existing.keys())
            enumerated_paths = {info.path for info in file_infos}
            stale_paths = [p for p in existing_paths if p not in enumerated_paths]
            deleted_marked = 0
            deleted_examples: list[str] = []
            if stale_paths:
                deleted_examples = stale_paths[:5]
                ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                scur.executemany(
                    "UPDATE files SET deleted=1, deleted_ts=? WHERE path=? AND deleted=0",
                    [(ts, path) for path in stale_paths],
                )
                shard.commit()
                deleted_marked = len(stale_paths)

            pending: list[FileInfo] = []
            unchanged: list[FileInfo] = []
            for info in file_infos:
                prev = existing.get(info.path)
                if self.full_rescan:
                    pending.append(info)
                elif prev is None or prev.get("deleted"):
                    pending.append(info)
                else:
                    if prev.get("size_bytes") != info.size_bytes or prev.get("mtime_utc") != info.mtime_utc:
                        pending.append(info)
                    else:
                        unchanged.append(info)

            if unchanged:
                scur.executemany(
                    "UPDATE files SET deleted=0, deleted_ts=NULL WHERE path=?",
                    [(info.path,) for info in unchanged],
                )
                shard.commit()

            resume_index = -1
            if self.resume_enabled and resume_state:
                last_path = resume_state.get("last_path_processed")
                if last_path:
                    for idx, info in enumerate(pending):
                        if info.path == last_path:
                            resume_index = idx
                            break

            already_processed = max(0, resume_index + 1)
            work_items = pending[already_processed:] if already_processed else list(pending)

            def _av_credit(path: str) -> int:
                prev = existing.get(path)
                if not prev:
                    return 0
                if int(prev.get("is_av") or 0) != 1:
                    return 0
                ok = prev.get("integrity_ok")
                return 1 if ok in (0, 1) else 0

            done_av = sum(_av_credit(info.path) for info in unchanged)
            done_av += sum(_av_credit(info.path) for info in pending[:already_processed])
            processed_all = len(unchanged) + already_processed

            mode_text = "Full" if self.full_rescan else "Delta"
            summary_msg = (
                f"AV {total_av} / Total {total_all}; pending {len(pending)} "
                f"(unchanged {len(unchanged)}, deleted {deleted_marked})"
            )

            with sqlite3.connect(self.db_catalog) as catalog:
                ccur = catalog.cursor()
                ccur.execute(
                    """
                    INSERT INTO drives(label, mount_path, fs_format, total_bytes, free_bytes, smart_scan, scanned_at, drive_type, notes, scan_mode)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(label) DO UPDATE SET
                        mount_path=excluded.mount_path,
                        fs_format=excluded.fs_format,
                        total_bytes=excluded.total_bytes,
                        free_bytes=excluded.free_bytes,
                        smart_scan=excluded.smart_scan,
                        scanned_at=excluded.scanned_at,
                        drive_type=excluded.drive_type,
                        notes=excluded.notes,
                        scan_mode=excluded.scan_mode
                    """,
                    (
                        self.label,
                        str(mount),
                        None,
                        total_bytes,
                        free_bytes,
                        smart_blob,
                        started_at,
                        self.dtype,
                        self.notes,
                        mode_text,
                    ),
                )
                ccur.execute(
                    """
                    INSERT INTO jobs(drive_label, started_at, status, total_av, total_all, done_av, duration_sec, message)
                    VALUES(?, datetime('now'), 'Running', ?, ?, ?, NULL, ?)
                    """,
                    (
                        self.label,
                        total_av,
                        total_all,
                        done_av,
                        summary_msg,
                    ),
                )
                self.job_id = ccur.lastrowid
                catalog.commit()

            log(
                f"[Job {self.job_id}] {mode_text} scan — total={total_all} AV={total_av} "
                f"pending={len(pending)} unchanged={len(unchanged)} deleted={deleted_marked}"
            )
            if deleted_marked:
                log(
                    f"[Job {self.job_id}] marked {deleted_marked} file(s) as deleted"
                    + (f"; examples: {', '.join(deleted_examples)}" if deleted_examples else "")
                )
            self._put({"type": "refresh_jobs"})

            self._put({"type": "status", "message": "Scanning files…"})
            self._current_phase = "hashing" if self.blake_for_av else "mediainfo"
            self._emit_progress(
                self._current_phase,
                processed_all,
                done_av,
                total_all=total_all,
                force=True,
            )

            max_workers = max(1, min(self.max_workers, 32))
            log(f"[Job {self.job_id}] scanning with {max_workers} worker thread(s)")

            def process_entry(info: FileInfo):
                if self.stop_evt.is_set():
                    return None
                path = info.path
                try:
                    stat = Path(path).stat()
                    size = int(stat.st_size)
                    mtime = _iso(stat.st_mtime)
                    is_av = 1 if info.is_av else 0
                    hb3 = None
                    if info.is_av and self.blake_for_av:
                        hb3 = blake3_hash(path)
                    mediainfo_path = self.tool_paths.get("mediainfo")
                    media_obj = mediainfo_json(path, mediainfo_path) if info.is_av else None
                    if media_obj is None:
                        integrity = None if info.is_av else 1
                        media_blob = None
                    else:
                        integrity = 0 if media_obj.get("error") else 1
                        media_blob = json.dumps(media_obj, ensure_ascii=False)
                    if info.is_av:
                        ffmpeg_ok = ffmpeg_verify(path, self.tool_paths.get("ffmpeg"))
                        if ffmpeg_ok is False:
                            integrity = 0
                    av_inc = 1 if is_av and integrity in (0, 1) else 0
                    return ((path, size, is_av, hb3, media_blob, integrity, mtime), av_inc)
                except Exception as exc:
                    log(f"[Job {self.job_id}] error on {path}: {exc}")
                    return (
                        (
                            path,
                            None,
                            1 if info.is_av else 0,
                            None,
                            json.dumps({"error": str(exc)}, ensure_ascii=False),
                            0,
                            None,
                        ),
                        0,
                    )

            def commit_result(row_tuple, av_increment: int):
                nonlocal processed_all, done_av
                if row_tuple is None:
                    return
                path, size, is_av, hb3, media_blob, integrity, mtime = row_tuple
                scur.execute(
                    """
                    INSERT INTO files(path,size_bytes,is_av,hash_blake3,media_json,integrity_ok,mtime_utc,deleted,deleted_ts)
                    VALUES(?,?,?,?,?,?,?,0,NULL)
                    ON CONFLICT(path) DO UPDATE SET
                        size_bytes=excluded.size_bytes,
                        is_av=excluded.is_av,
                        hash_blake3=excluded.hash_blake3,
                        media_json=excluded.media_json,
                        integrity_ok=excluded.integrity_ok,
                        mtime_utc=excluded.mtime_utc,
                        deleted=0,
                        deleted_ts=NULL
                    """,
                    (path, size, is_av, hb3, media_blob, integrity, mtime),
                )
                shard.commit()
                if light_pipeline and path and size is not None:
                    try:
                        light_pipeline.process(SimpleNamespace(path=path, fs_path=path), metadata=None)
                    except Exception:
                        pass
                processed_all += 1
                if av_increment:
                    done_av += av_increment
                if self.resume_enabled:
                    state_store.checkpoint(self._current_phase, path)
                self._emit_progress(
                    self._current_phase,
                    processed_all,
                    done_av,
                    total_all=total_all,
                    force=(processed_all % 200 == 0 or processed_all == total_all),
                )

            t0 = time.perf_counter()

            if work_items and max_workers > 1:
                executor = ThreadPoolExecutor(max_workers=max_workers)
                try:
                    in_flight = {}
                    buffered: Dict[int, Optional[tuple]] = {}
                    submit_idx = 0
                    commit_idx = 0
                    total_items = len(work_items)
                    while (submit_idx < total_items or in_flight) and not self.stop_evt.is_set():
                        while submit_idx < total_items and len(in_flight) < max_workers:
                            info = work_items[submit_idx]
                            future = executor.submit(process_entry, info)
                            in_flight[future] = submit_idx
                            submit_idx += 1
                        if not in_flight:
                            break
                        done_futures, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                        for future in done_futures:
                            idx = in_flight.pop(future)
                            try:
                                buffered[idx] = future.result()
                            except Exception as exc:
                                log(f"[Job {self.job_id}] worker failure: {exc}")
                                buffered[idx] = None
                        while commit_idx in buffered:
                            result = buffered.pop(commit_idx)
                            if result and not self.stop_evt.is_set():
                                row_data, av_inc = result
                                commit_result(row_data, av_inc)
                            commit_idx += 1
                        if self.stop_evt.is_set():
                            break
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
            else:
                for info in work_items:
                    if self.stop_evt.is_set():
                        break
                    result = process_entry(info)
                    if result:
                        row_data, av_inc = result
                        commit_result(row_data, av_inc)

            if self.resume_enabled:
                if work_items:
                    state_store.checkpoint(self._current_phase, work_items[-1].path, force=True)
                state_store.checkpoint("finalizing", None, force=True)
                state_store.clear()

            self._current_phase = "finalizing"
            self._emit_progress(self._current_phase, processed_all, done_av, total_all=total_all, force=True)

            duration = int(time.perf_counter() - t0)
            status = "Stopped" if self.stop_evt.is_set() else "Done"
            if light_pipeline is not None:
                light_summary = light_pipeline.finalize()

        with sqlite3.connect(self.db_catalog) as catalog:
            catalog.execute(
                """UPDATE jobs SET finished_at=datetime('now'), status=?, done_av=?, duration_sec=?, message=? WHERE id=?""",
                (
                    status,
                    done_av,
                    duration,
                    f"Completed ({done_av}/{total_av}); pending {len(pending)}; deleted {deleted_marked}",
                    self.job_id,
                ),
            )
            catalog.commit()

        self._put({"type": "refresh_jobs"})
        final_msg = "Scan stopped." if status == "Stopped" else "Scan complete."
        self._put({"type": "status", "message": final_msg})
        self._put({
            "type": "done",
            "status": status,
            "total_all": total_all,
            "done_av": done_av,
            "total_av": total_av,
            "duration": duration,
            "light_analysis": light_summary,
        })


class CatalogExportWorker(threading.Thread):
    def __init__(
        self,
        drive_label: str,
        shard_path: Path,
        fmt: str,
        include_deleted: bool,
        av_only: bool,
        event_queue: "queue.Queue[dict]",
    ):
        super().__init__(daemon=True)
        self.drive_label = drive_label
        self.shard_path = Path(shard_path)
        self.format = fmt.lower()
        self.filters = ExportFilters(include_deleted=include_deleted, av_only=av_only, since_utc=None)
        self.queue = event_queue
        self.token = f"export-{id(self)}"

    def _put(self, payload: dict):
        data = {
            "type": payload.get("type"),
            "format": self.format,
            "drive_label": self.drive_label,
            "token": self.token,
        }
        data.update(payload)
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            pass

    def _on_progress(self, rows: int):
        self._put({"type": "export_progress", "rows": rows})

    def run(self):
        filters_desc = (
            f"include_deleted={str(self.filters.include_deleted).lower()}, "
            f"av_only={str(self.filters.av_only).lower()}, "
            f"since={self.filters.since_utc or '—'}"
        )
        log(f"[Export] {self.drive_label} → {self.format.upper()} starting ({filters_desc})")
        try:
            result = export_shard(
                self.shard_path,
                WORKING_DIR_PATH,
                self.drive_label,
                self.filters,
                fmt=self.format,
                progress_callback=self._on_progress,
            )
        except Exception as exc:
            log(f"[Export] {self.drive_label} → {self.format.upper()} failed: {exc}")
            self._put({"type": "export_error", "message": str(exc)})
            return
        log(
            f"[Export] {self.drive_label} → {self.format.upper()} finished: {result.rows} rows -> {result.path}"
        )
        self._put({"type": "export_done", "path": str(result.path), "count": result.rows})


class MaintenanceProgressDialog(Toplevel):
    def __init__(self, parent: Toplevel, message: str, on_cancel: Callable[[], None]):
        super().__init__(parent)
        self.title("Maintenance in progress…")
        self.transient(parent)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._on_cancel_cb = on_cancel
        container = ttk.Frame(self, padding=(20, 16), style="Content.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.message_var = StringVar(value=message)
        ttk.Label(
            container,
            textvariable=self.message_var,
            style="Subtle.TLabel",
            wraplength=360,
        ).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(
            container,
            orient="horizontal",
            mode="indeterminate",
            length=320,
            style="Info.Horizontal.TProgressbar",
        )
        self.progress.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        self.progress.start(100)
        self.cancel_button = ttk.Button(
            container,
            text="Cancel",
            command=self._on_cancel,
            style="Danger.TButton",
        )
        self.cancel_button.grid(row=2, column=0, sticky="e")

    def update_message(self, message: str) -> None:
        self.message_var.set(message)

    def _on_cancel(self) -> None:
        if self._on_cancel_cb:
            try:
                self._on_cancel_cb()
            except Exception:
                pass
        self.cancel_button.configure(state="disabled")
        self.message_var.set("Cancel requested…")

    def close(self) -> None:
        try:
            self.progress.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


class MaintenanceDialog(Toplevel):
    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("Database Maintenance")
        self.transient(app.root)
        self.grab_set()
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.options = self._load_options()
        self.targets: List[MaintenanceTarget] = []
        self.worker_thread: Optional[threading.Thread] = None
        self.cancel_event: Optional[threading.Event] = None
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.progress_dialog: Optional[MaintenanceProgressDialog] = None
        self.status_var = StringVar(value="Ready.")
        self._running_action: Optional[str] = None
        self._scan_active = bool(self.app.scan_in_progress)
        self._active_scan_safe = safe_label(self.app.label_var.get()) if hasattr(self.app, "label_var") else ""
        self._tooltips: Dict[str, SimpleTooltip] = {}

        self._build_ui()
        self.refresh_targets()
        self.after(200, self._poll_queue)

    def _load_options(self) -> MaintenanceOptions:
        return resolve_options(load_settings(WORKING_DIR_PATH))

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        container = ttk.Frame(self, padding=(18, 16), style="Content.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)

        ttk.Label(
            container,
            text="Select a database and run maintenance actions.",
            style="Subtle.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        tree_frame = ttk.Frame(container, style="Card.TFrame")
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("name", "kind", "size", "path"),
            show="headings",
            height=6,
        )
        self.tree.heading("name", text="Database")
        self.tree.heading("kind", text="Type")
        self.tree.heading("size", text="Size")
        self.tree.heading("path", text="Path")
        self.tree.column("name", width=240, anchor="w")
        self.tree.column("kind", width=80, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("path", width=320, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _evt: self._update_button_states())

        buttons = ttk.Frame(container, style="Card.TFrame")
        buttons.grid(row=2, column=0, sticky="ew", pady=(12, 4))
        for idx in range(5):
            buttons.columnconfigure(idx, weight=1)

        self.buttons: Dict[str, ttk.Button] = {}
        self.buttons["quick"] = ttk.Button(
            buttons,
            text="Quick Optimize",
            command=lambda: self._start_action("quick"),
        )
        self.buttons["quick"].grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self.buttons["integrity"] = ttk.Button(
            buttons,
            text="Check Integrity",
            command=lambda: self._start_action("integrity"),
        )
        self.buttons["integrity"].grid(row=0, column=1, padx=(0, 8), sticky="ew")
        self.buttons["full"] = ttk.Button(
            buttons,
            text="Full Maintenance",
            command=lambda: self._start_action("full"),
            style="Accent.TButton",
        )
        self.buttons["full"].grid(row=0, column=2, padx=(0, 8), sticky="ew")
        self.buttons["vacuum"] = ttk.Button(
            buttons,
            text="VACUUM (force)",
            command=lambda: self._start_action("vacuum", force=True),
        )
        self.buttons["vacuum"].grid(row=0, column=3, padx=(0, 8), sticky="ew")
        self.buttons["backup"] = ttk.Button(
            buttons,
            text="Backup Now",
            command=lambda: self._start_action("backup"),
        )
        self.buttons["backup"].grid(row=0, column=4, sticky="ew")
        for key, button in self.buttons.items():
            self._tooltips[key] = SimpleTooltip(button)

        log_frame = ttk.LabelFrame(container, text="Maintenance log", padding=(12, 10), style="Card.TLabelframe")
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_widget = ScrolledText(
            log_frame,
            height=10,
            width=100,
            state="disabled",
            relief="flat",
            borderwidth=0,
            background=self.app.colors["background"],
            foreground=self.app.colors["text"],
            font=("Consolas", 10),
        )
        self.log_widget.grid(row=0, column=0, sticky="nsew")

        ttk.Label(container, textvariable=self.status_var, style="Subtle.TLabel").grid(
            row=4, column=0, sticky="w", pady=(8, 0)
        )

        self._update_button_states()

    def _append_log(self, line: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert(END, line + "\n")
        self.log_widget.see(END)
        self.log_widget.configure(state="disabled")

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)
        self.app._set_status(message, "accent")

    def refresh_targets(self) -> None:
        previous = self.tree.selection()
        selected_id = previous[0] if previous else None
        self.tree.delete(*self.tree.get_children())
        self.targets = []

        mapping: Dict[str, str] = {}
        catalog_path = Path(self.app.db_path.get())
        try:
            with sqlite3.connect(self.app.db_path.get()) as conn:
                rows = conn.execute("SELECT label FROM drives").fetchall()
                for (label,) in rows:
                    mapping[safe_label(label)] = label
        except Exception:
            pass

        def _insert_target(target: MaintenanceTarget) -> None:
            display_name = target.name
            if self._is_scan_active(target):
                display_name += " (scan active)"
            size_text = format_bytes(target.size_bytes)
            self.tree.insert(
                "",
                END,
                iid=target.identifier,
                values=(display_name, target.kind.title(), size_text, str(target.path)),
            )

        if catalog_path.exists():
            catalog_size = database_size_bytes(catalog_path)
        else:
            catalog_size = 0
        catalog_target = MaintenanceTarget(
            identifier="catalog",
            name="Catalog database",
            path=catalog_path,
            kind="catalog",
            size_bytes=catalog_size,
            label="Catalog",
            safe_label="catalog",
        )
        self.targets.append(catalog_target)
        _insert_target(catalog_target)

        shards_dir = SHARDS_DIR_PATH
        try:
            shards_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        for shard_path in sorted(Path(shards_dir).glob("*.db")):
            safe_name = shard_path.stem
            display = mapping.get(safe_name) or safe_name.replace("_", " ")
            size = database_size_bytes(shard_path) if shard_path.exists() else 0
            target = MaintenanceTarget(
                identifier=f"shard:{safe_name}",
                name=f"Shard — {display}",
                path=shard_path,
                kind="shard",
                size_bytes=size,
                label=display,
                safe_label=safe_name,
            )
            self.targets.append(target)
            _insert_target(target)

        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
        elif self.targets:
            self.tree.selection_set(self.targets[0].identifier)
        self._update_button_states()

    def _get_selected_target(self) -> Optional[MaintenanceTarget]:
        selection = self.tree.selection()
        if not selection:
            return None
        selected = selection[0]
        for target in self.targets:
            if target.identifier == selected:
                return target
        return None

    def _is_scan_active(self, target: Optional[MaintenanceTarget]) -> bool:
        if not target:
            return False
        if not self._scan_active:
            return False
        if target.kind == "catalog":
            return True
        return bool(self._active_scan_safe and self._active_scan_safe == target.safe_label)

    def _update_button_states(self) -> None:
        target = self._get_selected_target()
        running = bool(self.worker_thread and self.worker_thread.is_alive())
        active_scan = self._is_scan_active(target)
        for key, button in self.buttons.items():
            tooltip = self._tooltips.get(key)
            if running:
                button.configure(state="disabled")
                if tooltip:
                    tooltip.set_text("Maintenance already running.")
            elif target is None:
                button.configure(state="disabled")
                if tooltip:
                    tooltip.set_text("Select a database first.")
            elif active_scan:
                button.configure(state="disabled")
                if tooltip:
                    tooltip.set_text("Disabled while a scan is active for this database.")
            else:
                button.configure(state="normal")
                if tooltip:
                    tooltip.set_text("")

    def _start_action(self, action: str, *, force: bool = False) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        target = self._get_selected_target()
        if target is None:
            messagebox.showinfo("Maintenance", "Please select a database first.")
            return
        if self._is_scan_active(target):
            messagebox.showwarning("Maintenance", "Cannot run maintenance while a scan is active for this database.")
            return
        self.options = self._load_options()
        self.cancel_event = threading.Event()
        self.worker_thread = threading.Thread(
            target=self._worker_main,
            args=(target, action, force),
            daemon=True,
        )
        self._running_action = action
        message = f"Running {action} on {target.label}…"
        self.progress_dialog = MaintenanceProgressDialog(self, message, self.cancel_event.set)
        self.worker_thread.start()
        self._set_status(message)
        self._update_button_states()

    def _worker_main(self, target: MaintenanceTarget, action: str, force: bool) -> None:
        busy_ms = self.options.busy_timeout_ms
        thresholds = {"vacuum_free_bytes_min": self.options.vacuum_free_bytes_min}
        start_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.queue.put({"type": "log", "line": f"[{start_ts}] {action} started for {target.label}"})
        try:
            if action == "integrity":
                result = check_integrity(target.path, busy_timeout_ms=busy_ms)
                self.queue.put({"type": "result", "action": action, "target": target, "result": result})
                update_maintenance_metadata(
                    last_run=datetime.utcnow(),
                    last_integrity_ok=bool(result.get("ok")),
                    working_dir=WORKING_DIR_PATH,
                )
            elif action == "quick":
                if self.cancel_event and self.cancel_event.is_set():
                    raise RuntimeError("Cancelled")
                backup = light_backup(target.path, f"quick_{target.safe_label}", working_dir=WORKING_DIR_PATH, busy_timeout_ms=busy_ms)
                self.queue.put({"type": "log", "line": f"Backup created → {backup}"})
                if self.cancel_event and self.cancel_event.is_set():
                    raise RuntimeError("Cancelled")
                metrics = reindex_and_analyze(target.path, busy_timeout_ms=busy_ms)
                self.queue.put({"type": "result", "action": action, "target": target, "result": {"backup": str(backup), "metrics": metrics}})
                update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
            elif action == "full":
                backup = light_backup(target.path, f"full_{target.safe_label}", working_dir=WORKING_DIR_PATH, busy_timeout_ms=busy_ms)
                self.queue.put({"type": "log", "line": f"Backup created → {backup}"})
                metrics = reindex_and_analyze(target.path, busy_timeout_ms=busy_ms)
                self.queue.put({"type": "log", "line": "Reindex & ANALYZE completed."})
                if self.cancel_event and self.cancel_event.is_set():
                    raise RuntimeError("Cancelled")
                vacuum_result = vacuum_if_needed(
                    target.path,
                    thresholds=thresholds,
                    busy_timeout_ms=busy_ms,
                    force=force,
                    active_check=lambda: self._scan_active,
                )
                self.queue.put(
                    {
                        "type": "result",
                        "action": action,
                        "target": target,
                        "result": {
                            "backup": str(backup),
                            "metrics": metrics,
                            "vacuum": vacuum_result,
                        },
                    }
                )
                update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
            elif action == "vacuum":
                backup = light_backup(target.path, f"vacuum_{target.safe_label}", working_dir=WORKING_DIR_PATH, busy_timeout_ms=busy_ms)
                self.queue.put({"type": "log", "line": f"Backup created → {backup}"})
                vacuum_result = vacuum_if_needed(
                    target.path,
                    thresholds=thresholds,
                    busy_timeout_ms=busy_ms,
                    force=force,
                    active_check=lambda: self._scan_active,
                )
                self.queue.put({"type": "result", "action": action, "target": target, "result": {"backup": str(backup), "vacuum": vacuum_result}})
                update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
            elif action == "backup":
                backup = light_backup(target.path, f"manual_{target.safe_label}", working_dir=WORKING_DIR_PATH, busy_timeout_ms=busy_ms)
                self.queue.put({"type": "result", "action": action, "target": target, "result": {"backup": str(backup)}})
            else:
                raise RuntimeError(f"Unknown maintenance action: {action}")
        except RuntimeError as exc:
            if "Cancelled" in str(exc):
                self.queue.put({"type": "log", "line": "Maintenance cancelled."})
                self.queue.put({"type": "cancelled"})
            else:
                self.queue.put({"type": "error", "message": str(exc)})
        except Exception as exc:
            self.queue.put({"type": "error", "message": str(exc)})
        finally:
            self.queue.put({"type": "done"})

    def _poll_queue(self) -> None:
        try:
            while True:
                event = self.queue.get_nowait()
                etype = event.get("type")
                if etype == "log":
                    self._append_log(event.get("line", ""))
                elif etype == "result":
                    target: MaintenanceTarget = event.get("target")
                    action = event.get("action")
                    result = event.get("result", {})
                    self._handle_result(target, action, result)
                elif etype == "error":
                    message = event.get("message") or "Maintenance failed."
                    self._append_log(f"ERROR — {message}")
                    messagebox.showerror("Maintenance", message)
                elif etype == "cancelled":
                    self._append_log("Cancelled by user.")
                    self._set_status("Maintenance cancelled.")
                elif etype == "done":
                    self._on_worker_done()
                if self.progress_dialog and etype in {"log", "result"}:
                    self.progress_dialog.update_message(self.status_var.get())
        except queue.Empty:
            pass
        finally:
            if self.winfo_exists():
                self.after(200, self._poll_queue)

    def _handle_result(self, target: MaintenanceTarget, action: str, result: Dict[str, object]) -> None:
        if action == "integrity":
            ok = bool(result.get("ok"))
            issues = result.get("issues") or []
            summary = "Integrity OK" if ok else f"Integrity FAILED ({len(issues)} issues)"
            self._append_log(summary)
            for issue in list(issues)[:10]:
                self._append_log(f"  • {issue}")
            if ok:
                self._set_status(f"Integrity OK for {target.label}.")
                self.app.show_banner(f"Integrity OK for {target.label}.", "SUCCESS")
            else:
                self._set_status(f"Integrity issues found for {target.label}.")
                self.app.show_banner(
                    f"Integrity issues detected for {target.label}. Make a backup, run full maintenance, then retry.",
                    "ERROR",
                )
        elif action in {"quick", "full"}:
            metrics = result.get("metrics") or {}
            backup = result.get("backup")
            if backup:
                self._append_log(f"Backup stored at {backup}")
            indexes = metrics.get("indexes_after")
            duration = metrics.get("duration_s")
            if isinstance(duration, (int, float)):
                self._append_log(f"Reindex completed — indexes={indexes} duration={duration:.2f}s")
            else:
                self._append_log("Reindex completed.")
            vac = result.get("vacuum")
            if isinstance(vac, dict):
                if vac.get("skipped"):
                    reason = vac.get("reason") or "threshold"
                    self._append_log(f"VACUUM skipped ({reason}).")
                else:
                    reclaimed = int(vac.get("reclaimed_bytes") or 0)
                    self._append_log(f"VACUUM reclaimed {format_bytes(reclaimed)}.")
            self._set_status(f"{action.title()} maintenance completed for {target.label}.")
        elif action == "vacuum":
            backup = result.get("backup")
            vac = result.get("vacuum") or {}
            if backup:
                self._append_log(f"Backup stored at {backup}")
            if vac.get("skipped"):
                self._append_log("VACUUM skipped (threshold not met).")
            else:
                reclaimed = int(vac.get("reclaimed_bytes") or 0)
                self._append_log(f"VACUUM reclaimed {format_bytes(reclaimed)}.")
            self._set_status(f"VACUUM finished for {target.label}.")
        elif action == "backup":
            backup = result.get("backup")
            self._append_log(f"Backup stored at {backup}")
            self._set_status(f"Backup complete for {target.label}.")

    def _on_worker_done(self) -> None:
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        self.worker_thread = None
        self.cancel_event = None
        self._running_action = None
        self.refresh_targets()
        self._set_status("Maintenance ready.")
        self._update_button_states()
        if self.app._maintenance_window is self:
            self.app._set_status("Ready.")

    def _on_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Maintenance", "Maintenance is running. Cancel and close?"):
                return
            if self.cancel_event:
                self.cancel_event.set()
        try:
            self.grab_release()
        except Exception:
            pass
        if self.app._maintenance_window is self:
            self.app._maintenance_window = None
        self.destroy()

    def on_scan_state_change(self, active: bool, label: Optional[str]) -> None:
        self._scan_active = bool(active)
        self._active_scan_safe = safe_label(label or "") if label else ""
        self.refresh_targets()

    def select_catalog(self) -> None:
        if self.tree.exists("catalog"):
            self.tree.selection_set("catalog")
            self.tree.see("catalog")
            self._update_button_states()


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
        self._maintenance_window: Optional[MaintenanceDialog] = None
        self._last_scan_label: Optional[str] = None
        self.structure_settings = load_structure_config(_SETTINGS if isinstance(_SETTINGS, dict) else {})
        self.structure_worker: Optional[threading.Thread] = None
        self.structure_status_var = StringVar(value="Structure profiling idle.")
        self.structure_summary_var = StringVar(value="No structure data available yet.")
        self._structure_action = "idle"

        # inputs
        self.path_var  = StringVar()
        self.label_var = StringVar()
        self.type_var  = StringVar()
        self.notes_var = StringVar()
        light_settings = {}
        if isinstance(_SETTINGS, dict):
            maybe_light = _SETTINGS.get("light_analysis")
            if isinstance(maybe_light, dict):
                light_settings = maybe_light
        light_default = bool(light_settings.get("enabled_default", False))
        gpu_settings: Dict[str, object] = {}
        if isinstance(_SETTINGS, dict):
            maybe_gpu = _SETTINGS.get("gpu")
            if isinstance(maybe_gpu, dict):
                gpu_settings = maybe_gpu
        policy_default = str(gpu_settings.get("policy", "AUTO") or "AUTO").upper()
        if policy_default not in {"AUTO", "FORCE_GPU", "CPU_ONLY"}:
            policy_default = "AUTO"
        allow_hwaccel_default = bool(gpu_settings.get("allow_hwaccel_video", True))
        try:
            self.gpu_min_vram = max(0, int(gpu_settings.get("min_free_vram_mb", 512) or 0))
        except Exception:
            self.gpu_min_vram = 512
        try:
            self.gpu_max_gpu_workers = max(
                1, int(gpu_settings.get("max_gpu_workers", 2) or 1)
            )
        except Exception:
            self.gpu_max_gpu_workers = 2
        self.blake_var = IntVar(value=0)
        self.light_analysis_var = IntVar(value=1 if light_default else 0)
        self.gpu_policy_var = StringVar(value=policy_default)
        self.gpu_hwaccel_var = IntVar(value=1 if allow_hwaccel_default else 0)
        self.gpu_status_var = StringVar(value="Probing GPU…")
        self._last_gpu_caps: Dict[str, object] = {}
        self._gpu_dialog: Optional["GPUProvisioningDialog"] = None
        self._gpu_prompted = False
        self.inventory_var = IntVar(value=0)
        self.rescan_mode_var = StringVar(value="delta")
        self.resume_var = IntVar(value=1)
        cpu_count = os.cpu_count() or 1
        default_threads = min(8, max(1, (cpu_count // 2) or 1))
        self.threads_var = IntVar(value=default_threads)
        self.log_lines: list[str] = []

        # worker state
        self.stop_evt: Optional[threading.Event] = None
        self.worker: Optional[ScannerWorker] = None
        self.worker_queue: "queue.Queue[dict]" = queue.Queue()
        self._closing = False
        self._export_workers: dict[str, "CatalogExportWorker"] = {}
        self._recent_export_paths: list[Path] = []
        self._active_toasts: list[Toplevel] = []
        self._banner_action_callback = None

        self._search_placeholder = "Type part of a file name…"
        self.search_query_var = StringVar(value=self._search_placeholder)
        self.search_drive_var = StringVar()
        self.search_status_var = StringVar(value="Results: 0 (showing first 1,000) — idle")
        self._search_queue: "queue.Queue[dict]" = queue.Queue()
        self._search_thread: Optional[threading.Thread] = None
        self._search_cancel: Optional[threading.Event] = None
        self._search_rows: Dict[str, dict] = {}
        self._search_results: list[dict] = []
        self._search_drive_options: list[str] = []

        self.search_plus_mode_var = StringVar(value="semantic")
        self.search_plus_type_var = StringVar(value="Any")
        self.search_plus_date_var = StringVar(value="Any time")
        self.search_plus_status_var = StringVar(value="Search+ idle.")
        self._search_plus_queue: "queue.Queue[dict]" = queue.Queue()
        self._search_plus_thread: Optional[threading.Thread] = None
        self._search_plus_cancel: Optional[threading.Event] = None
        self._search_plus_rows: Dict[str, dict] = {}
        self._search_plus_results: list[dict] = []
        self._search_plus_images: list[Any] = []
        self._search_plus_service_vars: Dict[str, StringVar] = {}
        self._search_plus_token = 0
        self._search_plus_progress_running = False
        self._search_plus_placeholder_image: Optional[PhotoImage] = None

        self.reports_drive_var = StringVar()
        self.reports_topn_var = IntVar(value=20)
        self.reports_depth_var = IntVar(value=2)
        self.reports_days_var = IntVar(value=30)
        self.reports_status_var = StringVar(value="Reports idle.")
        self._reports_queue: "queue.Queue[dict]" = queue.Queue()
        self._reports_thread: Optional[threading.Thread] = None
        self._reports_cancel: Optional[threading.Event] = None
        self._reports_bundle: Optional[reports_util.ReportBundle] = None
        self._report_sections: dict[str, reports_util.SectionResult] = {}
        self._report_tree_rows: dict[str, dict[str, dict[str, Any]]] = {}
        self._report_sort_state: dict[tuple[str, str], bool] = {}
        self.report_trees: dict[str, ttk.Treeview] = {}
        self._reports_run_token = 0

        self.api_enabled_default = API_ENABLED_DEFAULT
        self.api_default_host = API_HOST_DEFAULT
        self.api_default_port = API_PORT_DEFAULT
        self.api_key_present = API_KEY_PRESENT_DEFAULT
        self.api_key_value = API_KEY_VALUE_DEFAULT
        self.api_process: Optional[subprocess.Popen] = None
        self.api_monitor_thread: Optional[threading.Thread] = None
        self.api_running = False
        self.api_stopping = False
        self.api_endpoint: Optional[str] = None
        self.api_status_var = StringVar(value=self._format_api_status(False))

        self._build_menu()
        self._build_form()
        self._build_tables()

        self.refresh_tool_statuses(initial=True)
        self._refresh_gpu_status()

        register_log_listener(self._log_enqueue)
        self._load_existing_logs()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._poll_worker_queue)
        self.root.after(200, self._poll_search_queue)
        self.root.after(200, self._poll_search_plus_queue)
        self.root.after(200, self._poll_reports_queue)

        for d in (SCANS_DIR, LOGS_DIR, EXPORTS_DIR, SHARDS_DIR):
            os.makedirs(d, exist_ok=True)
        init_catalog(self.db_path.get())
        self.refresh_all()
        self._update_status_line(force=True)
        self._schedule_status_ticker()

        if self.api_enabled_default:
            self.root.after(800, self.start_local_api)

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

    def _format_gpu_status(
        self,
        caps: Dict[str, object],
        provider: str,
        hwaccel_enabled: bool,
    ) -> str:
        if caps.get("has_nvidia"):
            name = str(caps.get("nv_name") or "NVIDIA GPU")
            total_bytes = caps.get("nv_vram_bytes") or 0
            try:
                total_gib = float(total_bytes) / (1024 ** 3)
            except Exception:
                total_gib = 0.0
            vram_text = f"{total_gib:.1f} GiB" if total_gib > 0 else "n/a"
        else:
            name = "No NVIDIA GPU"
            vram_text = "0 GiB"
        driver = str(caps.get("nv_driver_version") or "n/a")
        label_map = {
            "CUDAExecutionProvider": "CUDA",
            "DmlExecutionProvider": "DirectML",
            "CPU": "CPU",
        }
        provider_label = label_map.get(provider, provider or "CPU")
        hwaccel_state = "on" if hwaccel_enabled else "off"
        summary = (
            f"Detected: {name} ({vram_text}), driver {driver}\n"
            f"ONNX provider: {provider_label} — hwaccel {hwaccel_state}"
        )
        error_text = caps.get("onnx_cuda_error")
        if error_text and not caps.get("onnx_cuda_ok"):
            snippet = str(error_text).strip().splitlines()[0]
            if len(snippet) > 120:
                snippet = snippet[:117] + "…"
            summary += f"\nCUDA unavailable: {snippet}"
        return summary

    def _refresh_gpu_status(self, *, force: bool = False, caps: Optional[Dict[str, object]] = None) -> None:
        try:
            snapshot = probe_gpu(refresh=force) if caps is None else caps
        except Exception:
            self.gpu_status_var.set("Detected: unknown, VRAM: n/a, ORT: CPU")
            return
        provider = select_provider(
            self.gpu_policy_var.get(),
            min_free_vram_mb=self.gpu_min_vram,
            caps=snapshot,
        )
        hwaccel_args = get_hwaccel_args(
            self.gpu_policy_var.get(),
            allow_hwaccel=bool(self.gpu_hwaccel_var.get()),
            caps=snapshot,
        )
        self.gpu_status_var.set(
            self._format_gpu_status(snapshot, provider, bool(hwaccel_args))
        )
        self._last_gpu_caps = dict(snapshot)
        if snapshot.get("onnx_cuda_ok"):
            self._gpu_prompted = False
            if self._gpu_dialog and self._gpu_dialog.winfo_exists():
                self._gpu_dialog.destroy()
                self._gpu_dialog = None
        else:
            if self._gpu_dialog and self._gpu_dialog.winfo_exists():
                self._gpu_dialog.update_caps(snapshot)
            else:
                self._maybe_prompt_cuda_help(snapshot)

    def _save_gpu_settings(self) -> None:
        payload = {
            "policy": (self.gpu_policy_var.get() or "AUTO").upper(),
            "allow_hwaccel_video": bool(self.gpu_hwaccel_var.get()),
            "min_free_vram_mb": int(self.gpu_min_vram),
            "max_gpu_workers": int(self.gpu_max_gpu_workers),
        }
        update_settings(WORKING_DIR_PATH, gpu=payload)

    def _on_gpu_policy_changed(self) -> None:
        value = (self.gpu_policy_var.get() or "AUTO").upper()
        if value not in {"AUTO", "FORCE_GPU", "CPU_ONLY"}:
            value = "AUTO"
        self.gpu_policy_var.set(value)
        self._save_gpu_settings()

    def _on_gpu_hwaccel_toggle(self) -> None:
        self._save_gpu_settings()

    def _maybe_prompt_cuda_help(self, caps: Dict[str, object]) -> None:
        if not sys.platform.startswith("win"):
            return
        if (self.gpu_policy_var.get() or "AUTO").upper() == "CPU_ONLY":
            return
        if not caps.get("has_nvidia"):
            return
        if caps.get("onnx_cuda_ok"):
            return
        if not caps.get("onnx_cuda_error"):
            return
        if self._gpu_prompted and not (
            self._gpu_dialog and self._gpu_dialog.winfo_exists()
        ):
            return
        if self._gpu_dialog and self._gpu_dialog.winfo_exists():
            self._gpu_dialog.update_caps(caps)
            return
        self._gpu_prompted = True
        self._gpu_dialog = GPUProvisioningDialog(self, caps)

    def open_gpu_troubleshooter(self) -> None:
        caps = self._last_gpu_caps
        if not caps:
            try:
                caps = probe_gpu()
            except Exception:
                caps = {}
        if self._gpu_dialog and self._gpu_dialog.winfo_exists():
            try:
                self._gpu_dialog.lift()
                self._gpu_dialog.focus_set()
            except Exception:
                pass
            self._gpu_dialog.update_caps(caps)
            return
        self._gpu_prompted = True
        self._gpu_dialog = GPUProvisioningDialog(self, caps)

    def _gpu_dialog_closed(self) -> None:
        self._gpu_dialog = None

    def use_directml_now(self, *, refresh: bool = False) -> None:
        try:
            caps = probe_gpu(refresh=refresh)
        except Exception:
            caps = self._last_gpu_caps
        if not caps or not caps.get("onnx_directml_ok"):
            messagebox.showwarning(
                "DirectML unavailable",
                "DirectML execution provider is not ready yet. Install the DirectML package or refresh after installing ONNX Runtime GPU dependencies.",
            )
            return
        self.gpu_policy_var.set("AUTO")
        self._save_gpu_settings()
        self._gpu_prompted = True
        self._refresh_gpu_status(force=False, caps=caps)
        messagebox.showinfo(
            "DirectML enabled",
            "DirectML will be used for inference until CUDA is ready. You can retry CUDA from the GPU panel at any time.",
        )
        if self._gpu_dialog and self._gpu_dialog.winfo_exists():
            try:
                self._gpu_dialog.destroy()
            except Exception:
                pass
            self._gpu_dialog = None

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
        mtools.add_command(label="Maintenance…", command=self.open_maintenance_dialog)
        mtools.add_separator()
        mtools.add_command(label="Diagnostics…", command=self.open_diagnostics_dialog)
        menubar.add_cascade(label="Tools", menu=mtools)

        mexports = Menu(menubar, tearoff=0)
        mexports.add_command(label="Export CSV…", command=lambda: self.request_export('csv'))
        mexports.add_command(label="Export JSONL…", command=lambda: self.request_export('jsonl'))
        mexports.add_separator()
        mexports.add_command(label="Open exports folder", command=self.open_exports_folder)
        menubar.add_cascade(label="Exports", menu=mexports)

    def open_maintenance_dialog(self) -> Optional[MaintenanceDialog]:
        if self._maintenance_window and self._maintenance_window.winfo_exists():
            try:
                self._maintenance_window.lift()
                self._maintenance_window.focus_set()
                self._maintenance_window.refresh_targets()
            except Exception:
                pass
            return self._maintenance_window
        dialog = MaintenanceDialog(self)
        self._maintenance_window = dialog
        return dialog

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
        self.banner_frame.columnconfigure(1, weight=0)
        self.banner_label = ttk.Label(
            self.banner_frame,
            textvariable=self.banner_message,
            anchor="w",
            style="BannerINFO.TLabel",
        )
        self.banner_label.grid(row=0, column=0, sticky="ew", padx=4, pady=2)
        self.banner_action = ttk.Button(self.banner_frame, text="", style="Accent.TButton", command=lambda: None)
        self.banner_action.grid(row=0, column=1, sticky="e", padx=(8, 12))
        self.banner_action.grid_remove()
        self.banner_frame.grid_remove()

        self.content = ttk.Frame(self.root, padding=(20, 18), style="Content.TFrame")
        self.content.grid(row=1, column=0, sticky=(N, S, E, W))
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(1, weight=1)

        header = ttk.Frame(self.content, style="Header.TFrame", padding=(0, 0, 0, 16))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)
        ttk.Label(header, text="Disk Scanner Catalog", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Manage your drives and follow scans at a glance.",
            style="HeaderSubtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        toolbar = ttk.Frame(header, style="Header.TFrame")
        toolbar.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Button(toolbar, text="Search", command=self.focus_search_tab).grid(row=0, column=0, padx=(12, 0))

        self.main_notebook = ttk.Notebook(self.content)
        self.main_notebook.grid(row=1, column=0, sticky="nsew")
        self.dashboard_tab = ttk.Frame(self.main_notebook, style="Content.TFrame")
        self.dashboard_tab.columnconfigure(0, weight=1)
        self.main_notebook.add(self.dashboard_tab, text="Dashboard")
        self.search_tab = ttk.Frame(self.main_notebook, style="Content.TFrame")
        self.search_tab.columnconfigure(0, weight=1)
        self.search_tab.rowconfigure(1, weight=1)
        self.main_notebook.add(self.search_tab, text="Search")
        self.search_plus_tab = ttk.Frame(self.main_notebook, style="Content.TFrame")
        self.search_plus_tab.columnconfigure(0, weight=1)
        self.search_plus_tab.rowconfigure(1, weight=1)
        self.main_notebook.add(self.search_plus_tab, text="Search+")
        self.reports_tab = ttk.Frame(self.main_notebook, style="Content.TFrame")
        self.reports_tab.columnconfigure(0, weight=1)
        for idx in range(1, 6):
            self.reports_tab.rowconfigure(idx, weight=1)
        self.main_notebook.add(self.reports_tab, text="Reports")
        self.structure_tab = ttk.Frame(self.main_notebook, style="Content.TFrame")
        self.structure_tab.columnconfigure(0, weight=1)
        self.structure_tab.rowconfigure(1, weight=1)
        self.main_notebook.add(self.structure_tab, text="Structure")

        self._build_dashboard_controls(self.dashboard_tab)
        self._build_search_tab(self.search_tab)
        self._build_search_plus_tab(self.search_plus_tab)
        self._build_reports_tab(self.reports_tab)
        self._build_structure_tab(self.structure_tab)

    def _build_dashboard_controls(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        db_frame = ttk.LabelFrame(parent, text="Database", padding=(16, 12), style="Card.TLabelframe")
        db_frame.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        for c in range(4):
            db_frame.columnconfigure(c, weight=0)
        db_frame.columnconfigure(1, weight=1)
        ttk.Label(db_frame, text="Current DB:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Label(db_frame, textvariable=self.db_path, style="Value.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Button(db_frame, text="Browse…", command=self.db_open, style="Accent.TButton").grid(row=0, column=2, sticky="e", padx=(12, 0))
        ttk.Button(db_frame, text="New DB…", command=self.db_new).grid(row=0, column=3, sticky="e", padx=(8, 0))

        api_controls = ttk.Frame(db_frame, style="Card.TFrame")
        api_controls.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        api_controls.columnconfigure(1, weight=1)
        self.api_toggle_button = ttk.Button(
            api_controls,
            text="Start Local API",
            command=self.toggle_local_api,
        )
        self.api_toggle_button.grid(row=0, column=0, sticky="w")
        ttk.Label(
            api_controls,
            textvariable=self.api_status_var,
            style="Subtle.TLabel",
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        scan_frame = ttk.LabelFrame(parent, text="Scan parameters", padding=(16, 16), style="Card.TLabelframe")
        scan_frame.grid(row=1, column=0, sticky="ew", pady=(0, 16))
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
        ttk.Checkbutton(
            scan_frame,
            text="Light Analysis (images & video thumbnails)",
            variable=self.light_analysis_var,
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Checkbutton(
            scan_frame,
            text="Inventory Only (skip hashing & media analysis)",
            variable=self.inventory_var,
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(0, 12))

        gpu_frame = ttk.LabelFrame(
            scan_frame, text="GPU", padding=(12, 10), style="Card.TLabelframe"
        )
        gpu_frame.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(0, 12))
        for col in range(4):
            gpu_frame.columnconfigure(col, weight=1 if col == 1 else 0)
        ttk.Label(
            gpu_frame,
            textvariable=self.gpu_status_var,
            style="Subtle.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Button(
            gpu_frame,
            text="Refresh",
            command=self._refresh_gpu_status,
        ).grid(row=0, column=2, sticky="e")
        ttk.Button(
            gpu_frame,
            text="Troubleshoot…",
            command=self.open_gpu_troubleshooter,
        ).grid(row=0, column=3, sticky="e", padx=(6, 0))
        ttk.Radiobutton(
            gpu_frame,
            text="Auto",
            value="AUTO",
            variable=self.gpu_policy_var,
            command=self._on_gpu_policy_changed,
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Radiobutton(
            gpu_frame,
            text="Force GPU",
            value="FORCE_GPU",
            variable=self.gpu_policy_var,
            command=self._on_gpu_policy_changed,
        ).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Radiobutton(
            gpu_frame,
            text="CPU Only",
            value="CPU_ONLY",
            variable=self.gpu_policy_var,
            command=self._on_gpu_policy_changed,
        ).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            gpu_frame,
            text="Use FFmpeg hwaccel when available",
            variable=self.gpu_hwaccel_var,
            command=self._on_gpu_hwaccel_toggle,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        rescan_options = ttk.Frame(scan_frame, style="Card.TFrame")
        rescan_options.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(0, 12))
        rescan_options.columnconfigure(0, weight=1)
        ttk.Radiobutton(
            rescan_options,
            text="Delta rescan (recommended)",
            value="delta",
            variable=self.rescan_mode_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            rescan_options,
            text="Full rescan (force re-hash everything)",
            value="full",
            variable=self.rescan_mode_var,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Checkbutton(
            rescan_options,
            text="Resume interrupted scan",
            variable=self.resume_var,
        ).grid(row=0, column=1, rowspan=2, sticky="w", padx=(24, 0))

        actions = ttk.Frame(scan_frame, style="Card.TFrame")
        actions.grid(row=10, column=0, columnspan=4, sticky="ew")
        for c in range(5):
            actions.columnconfigure(c, weight=1)
        ttk.Button(actions, text="Scan", command=self.start_scan, style="Accent.TButton").grid(row=0, column=0, sticky="ew")
        ttk.Button(actions, text="Rescan (delete shard)", command=self.rescan).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(actions, text="Stop", command=self.stop_scan, style="Danger.TButton").grid(row=0, column=2, sticky="ew", padx=(8, 0))
        ttk.Button(actions, text="Export catalog", command=self.export_db).grid(row=0, column=3, sticky="ew", padx=(8, 0))
        exports_menu_btn = ttk.Menubutton(actions, text="Exports")
        exports_menu = Menu(exports_menu_btn, tearoff=0)
        exports_menu.add_command(label="Export CSV…", command=lambda: self.request_export('csv'))
        exports_menu.add_command(label="Export JSONL…", command=lambda: self.request_export('jsonl'))
        exports_menu.add_separator()
        exports_menu.add_command(label="Open exports folder", command=self.open_exports_folder)
        exports_menu_btn["menu"] = exports_menu
        exports_menu_btn.grid(row=0, column=4, sticky="ew", padx=(8, 0))
        self.exports_menu = exports_menu
        self.exports_button = exports_menu_btn

        status_activity = ttk.Frame(scan_frame, style="Card.TFrame")
        status_activity.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(12, 0))
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

        for idx in (2, 3, 4, 5):
            parent.rowconfigure(idx, weight=1)

    def _build_search_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        controls = ttk.LabelFrame(parent, text="Quick Search", padding=(16, 16), style="Card.TLabelframe")
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Drive:").grid(row=0, column=0, sticky="w")
        self.search_drive_combo = ttk.Combobox(controls, textvariable=self.search_drive_var, state="readonly", values=())
        self.search_drive_combo.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        ttk.Label(controls, text="Query:").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.search_entry = ttk.Entry(controls, textvariable=self.search_query_var, width=48)
        self.search_entry.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(12, 0))
        self.search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self.search_entry.bind("<FocusOut>", self._on_search_focus_out)
        self.search_entry.bind("<Return>", lambda _evt: self.perform_search())
        self.search_entry.configure(foreground=self.colors.get("muted_text", "#94a3b8"))

        self.search_button = ttk.Button(controls, text="Search", command=self.perform_search, style="Accent.TButton")
        self.search_button.grid(row=1, column=2, sticky="ew", padx=(12, 0), pady=(12, 0))

        ttk.Label(controls, text="Type at least 3 characters.", style="Subtle.TLabel").grid(
            row=2,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(8, 0),
        )

        results_frame = ttk.LabelFrame(parent, text="Results", padding=(16, 12), style="Card.TLabelframe")
        results_frame.grid(row=1, column=0, sticky=(N, S, E, W))
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)

        columns = ("name", "category", "size", "modified", "drive", "path")
        self.search_tree = ttk.Treeview(results_frame, columns=columns, show="headings", height=12, style="Card.Treeview")
        headings = {
            "name": "Name",
            "category": "Category",
            "size": "Size",
            "modified": "Modified",
            "drive": "Drive",
            "path": "Path",
        }
        widths = {
            "name": 260,
            "category": 120,
            "size": 90,
            "modified": 150,
            "drive": 110,
            "path": 420,
        }
        for key in columns:
            self.search_tree.heading(key, text=headings[key])
            anchor = W if key in {"name", "category", "drive", "path"} else E
            self.search_tree.column(key, anchor=anchor, stretch=True, width=widths[key])
        search_scroll = ttk.Scrollbar(results_frame, orient="vertical", command=self.search_tree.yview)
        self.search_tree.configure(yscrollcommand=search_scroll.set)
        self.search_tree.grid(row=0, column=0, sticky=(N, S, E, W))
        search_scroll.grid(row=0, column=1, sticky=(N, S))

        self.search_tree.bind("<Double-1>", self._on_search_item_open)
        self.search_tree.bind("<Return>", self._on_search_item_open)
        self.search_tree.bind("<Button-3>", self._show_search_menu)
        self.search_tree.bind("<Control-Button-1>", self._show_search_menu)

        ttk.Label(results_frame, textvariable=self.search_status_var, style="Subtle.TLabel").grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(8, 0),
        )

        self.search_menu = Menu(self.search_tree, tearoff=0)
        self.search_menu.add_command(label="Open folder", command=self._open_selected_search_path)
        self.search_menu.add_command(label="Copy full path", command=self._copy_search_path)
        self.search_menu.add_command(label="Export results…", command=self._export_search_results)

        self._on_search_focus_out()

    def _build_search_plus_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        controls = ttk.LabelFrame(parent, text="Search+", padding=(16, 16), style="Card.TLabelframe")
        controls.grid(row=0, column=0, sticky="ew")
        for col in range(3):
            controls.columnconfigure(col, weight=1)
        controls.columnconfigure(3, weight=0)

        ttk.Label(controls, text="Query:").grid(row=0, column=0, columnspan=3, sticky="w")
        self.search_plus_query_widget = ScrolledText(controls, height=3, width=70, wrap="word")
        self.search_plus_query_widget.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.search_plus_query_widget.bind("<Control-Return>", lambda _evt: self.start_search_plus())
        self.search_plus_query_widget.bind("<Shift-Return>", lambda _evt: self.start_search_plus())
        self.search_plus_button = ttk.Button(
            controls,
            text="Run Search+",
            command=self.start_search_plus,
            style="Accent.TButton",
        )
        self.search_plus_button.grid(row=1, column=3, sticky="nsw", padx=(12, 0))

        ttk.Label(
            controls,
            text=(
                "Combines semantic index, transcript snippets, and inventory keyword results."
            ),
            style="Subtle.TLabel",
            wraplength=560,
            justify="left",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        mode_frame = ttk.Frame(controls, style="Card.TFrame")
        mode_frame.grid(row=3, column=0, columnspan=4, sticky="w", pady=(12, 0))
        ttk.Label(mode_frame, text="Mode:").grid(row=0, column=0, sticky="w")
        mode_options = [
            ("Semantic", "semantic"),
            ("Keyword", "keyword"),
            ("Hybrid", "hybrid"),
        ]
        for idx, (label, value) in enumerate(mode_options, start=1):
            ttk.Radiobutton(
                mode_frame,
                text=label,
                value=value,
                variable=self.search_plus_mode_var,
            ).grid(row=0, column=idx, sticky="w", padx=(12 if idx > 1 else 8, 0))

        filters_frame = ttk.Frame(controls, style="Card.TFrame")
        filters_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        filters_frame.columnconfigure(1, weight=1)
        filters_frame.columnconfigure(3, weight=1)

        ttk.Label(filters_frame, text="Type:").grid(row=0, column=0, sticky="w")
        type_values = ["Any", "Video", "Audio", "Image", "Document", "Archive", "Other"]
        self.search_plus_type_combo = ttk.Combobox(
            filters_frame,
            textvariable=self.search_plus_type_var,
            state="readonly",
            values=type_values,
            width=16,
        )
        if self.search_plus_type_var.get() not in type_values:
            self.search_plus_type_var.set("Any")
        self.search_plus_type_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))

        ttk.Label(filters_frame, text="Date:").grid(row=0, column=2, sticky="w", padx=(16, 0))
        date_values = [
            "Any time",
            "Last 7 days",
            "Last 30 days",
            "Last 180 days",
            "Last 365 days",
        ]
        self.search_plus_date_combo = ttk.Combobox(
            filters_frame,
            textvariable=self.search_plus_date_var,
            state="readonly",
            values=date_values,
            width=18,
        )
        if self.search_plus_date_var.get() not in date_values:
            self.search_plus_date_var.set("Any time")
        self.search_plus_date_combo.grid(row=0, column=3, sticky="w", padx=(8, 0))

        statuses_frame = ttk.Frame(controls, style="Card.TFrame")
        statuses_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        statuses_frame.columnconfigure(0, weight=1)
        self._search_plus_service_vars.clear()
        for idx, (key, label_text) in enumerate(
            (
                ("semantic", "Semantic index"),
                ("transcripts", "Transcripts"),
                ("inventory", "Inventory"),
            )
        ):
            var = StringVar(value=f"{label_text} — idle.")
            self._search_plus_service_vars[key] = var
            ttk.Label(statuses_frame, textvariable=var, style="Subtle.TLabel").grid(
                row=idx,
                column=0,
                sticky="w",
                pady=(0 if idx == 0 else 2, 0),
            )

        self.search_plus_progress = ttk.Progressbar(controls, mode="indeterminate")
        self.search_plus_progress.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(12, 0))

        if self._search_plus_placeholder_image is None:
            try:
                placeholder = PhotoImage(width=96, height=54)
                placeholder.put("#1f2937", to=(0, 0, 95, 53))
                self._search_plus_placeholder_image = placeholder
            except Exception:
                self._search_plus_placeholder_image = None

        results_frame = ttk.LabelFrame(
            parent,
            text="Results",
            padding=(16, 12),
            style="Card.TLabelframe",
        )
        results_frame.grid(row=1, column=0, sticky=(N, S, E, W), pady=(12, 0))
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)

        columns = ("title", "score", "drive", "sources", "snippet", "path")
        self.search_plus_tree = ttk.Treeview(
            results_frame,
            columns=columns,
            show="tree headings",
            height=12,
            style="Card.Treeview",
        )
        self.search_plus_tree.heading("#0", text="Preview")
        self.search_plus_tree.column("#0", anchor="center", width=96, stretch=False)
        headings = {
            "title": "Title",
            "score": "Score",
            "drive": "Drive",
            "sources": "Sources",
            "snippet": "Snippet",
            "path": "Path",
        }
        widths = {
            "title": 260,
            "score": 80,
            "drive": 110,
            "sources": 140,
            "snippet": 360,
            "path": 320,
        }
        for key in columns:
            self.search_plus_tree.heading(key, text=headings[key])
            anchor = W if key in {"title", "drive", "sources", "snippet", "path"} else E
            self.search_plus_tree.column(key, anchor=anchor, width=widths[key], stretch=True)
        search_plus_scroll = ttk.Scrollbar(results_frame, orient="vertical", command=self.search_plus_tree.yview)
        self.search_plus_tree.configure(yscrollcommand=search_plus_scroll.set)
        self.search_plus_tree.grid(row=0, column=0, sticky=(N, S, E, W))
        search_plus_scroll.grid(row=0, column=1, sticky=(N, S))

        self.search_plus_tree.bind("<Double-1>", self._on_search_plus_item_open)
        self.search_plus_tree.bind("<Return>", self._on_search_plus_item_open)
        self.search_plus_tree.bind("<<TreeviewSelect>>", self._on_search_plus_selection)
        self.search_plus_tree.bind("<Button-3>", self._show_search_plus_menu)
        self.search_plus_tree.bind("<Control-Button-1>", self._show_search_plus_menu)

        ttk.Label(
            results_frame,
            textvariable=self.search_plus_status_var,
            style="Subtle.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        action_frame = ttk.Frame(results_frame, style="Card.TFrame")
        action_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for col in range(4):
            action_frame.columnconfigure(col, weight=0)
        action_frame.columnconfigure(4, weight=1)

        self.search_plus_open_button = ttk.Button(
            action_frame,
            text="Open folder",
            command=self._open_selected_search_plus_path,
            state="disabled",
        )
        self.search_plus_open_button.grid(row=0, column=0, sticky="w")
        self.search_plus_copy_button = ttk.Button(
            action_frame,
            text="Copy path",
            command=self._copy_search_plus_path,
            state="disabled",
        )
        self.search_plus_copy_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.search_plus_transcript_button = ttk.Button(
            action_frame,
            text="Open transcript",
            command=self._open_selected_search_plus_transcript,
            state="disabled",
        )
        self.search_plus_transcript_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.search_plus_inventory_button = ttk.Button(
            action_frame,
            text="Open inventory detail",
            command=self._open_selected_search_plus_inventory,
            state="disabled",
        )
        self.search_plus_inventory_button.grid(row=0, column=3, sticky="w", padx=(8, 0))

        self.search_plus_menu = Menu(self.search_plus_tree, tearoff=0)
        self.search_plus_menu.add_command(
            label="Open folder",
            command=self._open_selected_search_plus_path,
        )
        self.search_plus_menu.add_command(
            label="Copy path",
            command=self._copy_search_plus_path,
        )
        self.search_plus_menu.add_command(
            label="Open transcript",
            command=self._open_selected_search_plus_transcript,
        )
        self.search_plus_menu.add_command(
            label="Open inventory detail",
            command=self._open_selected_search_plus_inventory,
        )

        self._on_search_plus_selection()

    def _build_reports_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        controls = ttk.LabelFrame(parent, text="Reports", padding=(16, 16), style="Card.TLabelframe")
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        ttk.Label(controls, text="Drive:").grid(row=0, column=0, sticky="w")
        self.reports_drive_combo = ttk.Combobox(
            controls,
            textvariable=self.reports_drive_var,
            state="readonly",
            values=(),
        )
        self.reports_drive_combo.grid(row=0, column=1, sticky="ew", padx=(12, 12))

        ttk.Label(controls, text="Top N:").grid(row=0, column=2, sticky="e")
        self.reports_topn_spin = Spinbox(
            controls,
            from_=5,
            to=500,
            width=6,
            textvariable=self.reports_topn_var,
        )
        self.reports_topn_spin.grid(row=0, column=3, sticky="w")

        ttk.Label(controls, text="Depth:").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.reports_depth_spin = Spinbox(
            controls,
            from_=1,
            to=10,
            width=6,
            textvariable=self.reports_depth_var,
        )
        self.reports_depth_spin.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(12, 0))

        ttk.Label(controls, text="Last X days:").grid(row=1, column=2, sticky="e", pady=(12, 0))
        self.reports_days_spin = Spinbox(
            controls,
            from_=1,
            to=365,
            width=6,
            textvariable=self.reports_days_var,
        )
        self.reports_days_spin.grid(row=1, column=3, sticky="w", pady=(12, 0))

        actions = ttk.Frame(controls, style="Card.TFrame")
        actions.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(16, 0))
        for idx in range(3):
            actions.columnconfigure(idx, weight=1)
        ttk.Button(actions, text="Run", command=self.run_reports, style="Accent.TButton").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(actions, text="Export CSV…", command=lambda: self.export_reports("csv")).grid(
            row=0, column=1, sticky="ew", padx=(12, 0)
        )
        ttk.Button(actions, text="Export JSON…", command=lambda: self.export_reports("json")).grid(
            row=0, column=2, sticky="ew", padx=(12, 0)
        )

        ttk.Label(
            controls,
            textvariable=self.reports_status_var,
            style="Subtle.TLabel",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(12, 0))

        section_specs = [
            ("overview", "Overview", 6),
            ("top_extensions", "Top extensions", 8),
            ("largest_files", "Largest files", 10),
            ("heaviest_folders", "Heaviest folders", 8),
            ("recent_changes", "Recent changes", 10),
        ]
        for row_index, (key, title, height) in enumerate(section_specs, start=1):
            frame = ttk.LabelFrame(parent, text=title, padding=(16, 12), style="Card.TLabelframe")
            frame.grid(row=row_index, column=0, sticky=(N, S, E, W), pady=(12 if row_index > 1 else 16, 0))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            tree = ttk.Treeview(
                frame,
                columns=(),
                show="headings",
                height=height,
                style="Card.Treeview",
            )
            tree.grid(row=0, column=0, sticky=(N, S, E, W))
            scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            scroll.grid(row=0, column=1, sticky=(N, S))
            self.report_trees[key] = tree
            self._report_tree_rows[key] = {}

    def _build_structure_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        controls = ttk.LabelFrame(
            parent,
            text="Structure Profiling",
            padding=(16, 16),
            style="Card.TLabelframe",
        )
        controls.grid(row=0, column=0, sticky="ew")
        for column in range(4):
            controls.columnconfigure(column, weight=1 if column in (1, 2) else 0)

        ttk.Button(
            controls,
            text="Profile",
            command=lambda: self.start_structure_job(verify=False),
            style="Accent.TButton",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            controls,
            text="Profile + Verify",
            command=lambda: self.start_structure_job(verify=True),
        ).grid(row=0, column=1, sticky="ew", padx=(12, 0))
        ttk.Button(
            controls,
            text="Export Review…",
            command=self.export_structure_review,
        ).grid(row=0, column=2, sticky="ew", padx=(12, 0))
        ttk.Button(
            controls,
            text="Refresh",
            command=self.refresh_structure_view,
        ).grid(row=0, column=3, sticky="ew", padx=(12, 0))

        ttk.Label(
            controls,
            textvariable=self.structure_status_var,
            style="Subtle.TLabel",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(12, 0))

        review_frame = ttk.LabelFrame(
            parent,
            text="Manual Review Queue",
            padding=(16, 16),
            style="Card.TLabelframe",
        )
        review_frame.grid(row=1, column=0, sticky=(N, S, E, W), pady=(12, 0))
        review_frame.columnconfigure(0, weight=1)
        review_frame.rowconfigure(0, weight=1)

        columns = ("folder", "confidence", "title", "year", "issues")
        self.structure_tree = ttk.Treeview(
            review_frame,
            columns=columns,
            show="headings",
            height=12,
            style="Card.Treeview",
        )
        headings = {
            "folder": "Folder",
            "confidence": "Confidence",
            "title": "Title",
            "year": "Year",
            "issues": "Issues",
        }
        widths = {
            "folder": 320,
            "confidence": 90,
            "title": 220,
            "year": 70,
            "issues": 320,
        }
        for key in columns:
            self.structure_tree.heading(key, text=headings[key])
            anchor = W if key in {"folder", "title", "issues"} else E
            self.structure_tree.column(key, anchor=anchor, width=widths[key], stretch=True)

        review_scroll = ttk.Scrollbar(
            review_frame,
            orient="vertical",
            command=self.structure_tree.yview,
        )
        self.structure_tree.configure(yscrollcommand=review_scroll.set)
        self.structure_tree.grid(row=0, column=0, sticky=(N, S, E, W))
        review_scroll.grid(row=0, column=1, sticky=(N, S))

        summary_frame = ttk.Frame(parent, style="Card.TFrame")
        summary_frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(
            summary_frame,
            textvariable=self.structure_summary_var,
            style="Subtle.TLabel",
        ).grid(row=0, column=0, sticky="w")

    def _build_tables(self):
        parent = getattr(self, "dashboard_tab", self.content)
        parent.rowconfigure(2, weight=1)
        parent.rowconfigure(3, weight=1)
        parent.rowconfigure(4, weight=1)
        parent.rowconfigure(5, weight=1)

        cols = ("id","label","mount","type","notes","serial","model","totalGB")
        drives_frame = ttk.LabelFrame(parent, text="Drives in catalog", padding=(16, 12), style="Card.TLabelframe")
        drives_frame.grid(row=2, column=0, sticky=(N, S, E, W), pady=(0, 12))
        drives_frame.columnconfigure(0, weight=1)
        drives_frame.rowconfigure(0, weight=1)

        drives_container = ttk.Frame(drives_frame, style="Card.TFrame")
        drives_container.grid(row=0, column=0, sticky=(N, S, E, W))
        drives_container.columnconfigure(0, weight=1)
        drives_container.rowconfigure(0, weight=1)

    def start_structure_job(self, verify: bool) -> None:
        label = self.label_var.get().strip()
        mount = self.path_var.get().strip()
        if not (label and mount):
            messagebox.showerror("Missing", "Please fill Mount Path and Disk Label.")
            return
        args = [
            "--label",
            label,
            "--mount",
            mount,
            "--catalog-db",
            self.db_path.get(),
            "--structure-scan",
        ]
        if verify:
            args.append("--structure-verify")
        shard = shard_path_for(label)
        args.extend(["--shard-db", str(shard)])
        action = "verify" if verify else "profile"
        self._start_structure_thread(args, "Running structure profiling…", action)

    def export_structure_review(self) -> None:
        label = self.label_var.get().strip()
        if not label:
            messagebox.showerror("Missing", "Please fill Disk Label before exporting review queue.")
            return
        path = filedialog.asksaveasfilename(
            title="Export Structure Review",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        args = [
            "--label",
            label,
            "--catalog-db",
            self.db_path.get(),
            "--structure-export-review",
            path,
        ]
        shard = shard_path_for(label)
        args.extend(["--shard-db", str(shard)])
        self._start_structure_thread(args, "Exporting review queue…", "export")

    def _start_structure_thread(self, args: List[str], status: str, action: str) -> None:
        if self.structure_worker and self.structure_worker.is_alive():
            messagebox.showwarning("Busy", "A structure task is already running.")
            return
        self._structure_action = action
        self.structure_status_var.set(status)
        self.structure_worker = threading.Thread(
            target=self._run_structure_task,
            args=(list(args),),
            daemon=True,
        )
        self.structure_worker.start()

    def _run_structure_task(self, cli_args: List[str]) -> None:
        try:
            exit_code = scan_drive.main(cli_args)
            self.root.after(0, lambda: self._on_structure_finished(exit_code, None))
        except Exception as exc:
            self.root.after(0, lambda: self._on_structure_finished(-1, str(exc)))

    def _on_structure_finished(self, exit_code: int, error: Optional[str]) -> None:
        self.structure_worker = None
        action = getattr(self, "_structure_action", "profile")
        if exit_code == 0 and not error:
            if action == "export":
                self.structure_status_var.set("Review export completed.")
                messagebox.showinfo("Structure", "Manual review queue exported successfully.")
            else:
                self.structure_status_var.set("Structure profiling completed.")
                self.refresh_structure_view()
        else:
            message = error or f"Exited with code {exit_code}" if exit_code not in (0, None) else (error or "Unknown error")
            self.structure_status_var.set(f"Structure task failed: {message}")
            messagebox.showerror("Structure", f"Structure task failed: {message}")

    def refresh_structure_view(self) -> None:
        if not hasattr(self, "structure_tree"):
            return
        label = self.label_var.get().strip()
        self.structure_tree.delete(*self.structure_tree.get_children())
        if not label:
            self.structure_summary_var.set("Select a drive label to view structure data.")
            return
        summary, rows = self._load_structure_data(label)
        if summary is None:
            self.structure_summary_var.set("No structure profiling data available for this drive.")
        else:
            self.structure_summary_var.set(
                f"Folders: {summary['total']} — confident {summary['confident']} | "
                f"medium {summary['medium']} | low {summary['low']}"
            )
        if rows:
            for row in rows:
                issues = ", ".join(row.get("issues", [])) if row.get("issues") else ""
                self.structure_tree.insert(
                    "",
                    "end",
                    values=(
                        row.get("folder_path"),
                        f"{row.get('confidence', 0.0):.2f}",
                        row.get("parsed_title") or "",
                        row.get("parsed_year") or "",
                        issues,
                    ),
                )
            self.structure_status_var.set(f"{len(rows)} folders queued for manual review.")
        else:
            self.structure_status_var.set("Manual review queue is empty.")

    def _load_structure_data(self, label: str) -> Tuple[Optional[Dict[str, int]], List[Dict[str, object]]]:
        shard = shard_path_for(label)
        if not shard.exists():
            return None, []
        try:
            conn = sqlite3.connect(shard)
        except sqlite3.Error:
            return None, []
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='folder_profile'"
            )
            if cur.fetchone() is None:
                return None, []
            high = float(getattr(self.structure_settings, "high_threshold", 0.8))
            low = float(getattr(self.structure_settings, "low_threshold", 0.5))
            total = conn.execute("SELECT COUNT(*) FROM folder_profile").fetchone()[0]
            confident = conn.execute(
                "SELECT COUNT(*) FROM folder_profile WHERE confidence >= ?",
                (high,),
            ).fetchone()[0]
            medium = conn.execute(
                "SELECT COUNT(*) FROM folder_profile WHERE confidence >= ? AND confidence < ?",
                (low, high),
            ).fetchone()[0]
            low_count = conn.execute(
                "SELECT COUNT(*) FROM folder_profile WHERE confidence < ?",
                (low,),
            ).fetchone()[0]
            summary = {
                "total": int(total or 0),
                "confident": int(confident or 0),
                "medium": int(medium or 0),
                "low": int(low_count or 0),
            }
            rows: List[Dict[str, object]] = []
            cursor = conn.execute(
                """
                SELECT rq.folder_path, rq.confidence, fp.parsed_title, fp.parsed_year, fp.issues_json
                FROM review_queue AS rq
                LEFT JOIN folder_profile AS fp ON fp.folder_path = rq.folder_path
                ORDER BY rq.confidence ASC
                LIMIT 200
                """
            )
            for entry in cursor.fetchall():
                try:
                    issues = (
                        json.loads(entry["issues_json"])
                        if entry["issues_json"]
                        else []
                    )
                except Exception:
                    issues = []
                rows.append(
                    {
                        "folder_path": entry["folder_path"],
                        "confidence": float(entry["confidence"] or 0.0),
                        "parsed_title": entry["parsed_title"],
                        "parsed_year": entry["parsed_year"],
                        "issues": issues,
                    }
                )
        finally:
            conn.close()
        return summary, rows

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
        jobs_frame = ttk.LabelFrame(parent, text="Scan jobs", padding=(16, 12), style="Card.TLabelframe")
        jobs_frame.grid(row=3, column=0, sticky=(N, S, E, W), pady=(0, 12))
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

        progress_frame = ttk.LabelFrame(parent, text="Progress", padding=(16, 16), style="Card.TLabelframe")
        progress_frame.grid(row=4, column=0, sticky="ew", pady=(0, 12))
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

        self.performance_var = StringVar(value="Performance: —")
        ttk.Label(
            header,
            textvariable=self.performance_var,
            style="Subtle.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

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
        self.skipped_detail_var = StringVar(value="Skipped: perm=0, long=0, ignored=0")
        ttk.Label(progress_frame, textvariable=self.skipped_detail_var, style="Subtle.TLabel").grid(row=3, column=0, sticky="w", pady=(4, 0))

        log_frame = ttk.LabelFrame(parent, text="Live log", padding=(16, 12), style="Card.TLabelframe")
        log_frame.grid(row=5, column=0, sticky=(N, S, E, W))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_widget = ScrolledText(log_frame, height=9, width=100, state="disabled", relief="flat", borderwidth=0,
                                       background=self.colors["background"], foreground=self.colors["text"],
                                       insertbackground=self.colors["text"], font=("Consolas", 10))
        self.log_widget.grid(row=0, column=0, sticky=(N, S, E, W))
        self._set_status("Ready.")
        self._update_progress_styles(0, 0)


    def _on_search_focus_in(self, _event=None):
        if not hasattr(self, "search_entry"):
            return
        if self.search_query_var.get() == self._search_placeholder:
            self.search_entry.delete(0, END)
            self.search_entry.configure(foreground=self.colors.get("text", "#e2e8f0"))
            self.search_query_var.set("")

    def _on_search_focus_out(self, _event=None):
        if not hasattr(self, "search_entry"):
            return
        current = self.search_query_var.get()
        if not current:
            self.search_query_var.set(self._search_placeholder)
            self.search_entry.configure(foreground=self.colors.get("muted_text", "#94a3b8"))

    def focus_search_tab(self) -> None:
        try:
            self.main_notebook.select(self.search_tab)
        except Exception:
            return
        self.root.after(10, lambda: (self.search_entry.focus_set(), self._on_search_focus_in()))

    def perform_search(self) -> None:
        raw_query = self.search_query_var.get()
        if raw_query == self._search_placeholder:
            raw_query = ""
        try:
            query_parts = sanitize_query(raw_query)
        except ValueError as exc:
            message = str(exc)
            self.search_status_var.set(message)
            self.show_banner(message, "INFO")
            return

        drive_label = self.search_drive_var.get().strip()
        if not drive_label:
            message = "Select a drive to search."
            self.search_status_var.set(message)
            self.show_banner(message, "INFO")
            return

        shard = shard_path_for(drive_label)
        if not shard.exists():
            message = "No inventory found. Run Inventory first."
            self.search_status_var.set(message)
            self.show_banner(message, "ERROR")
            return

        if self._search_thread and self._search_thread.is_alive():
            if self._search_cancel:
                self._search_cancel.set()

        self._search_cancel = threading.Event()
        self._search_thread = threading.Thread(
            target=self._search_worker,
            args=(shard, drive_label, query_parts, self._search_cancel),
            daemon=True,
        )
        self._search_thread.start()
        self._search_results = []
        self._search_rows.clear()
        if hasattr(self, "search_tree"):
            self.search_tree.delete(*self.search_tree.get_children())
        self.search_status_var.set(f"Searching {drive_label}…")
        try:
            self.main_notebook.select(self.search_tab)
        except Exception:
            pass

    def _search_worker(
        self,
        shard_path: Path,
        drive_label: str,
        query_parts: Dict[str, str],
        cancel_event: threading.Event,
    ) -> None:
        migration_error: Optional[str] = None
        use_name = True
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(str(shard_path))
            connection.row_factory = sqlite3.Row
            try:
                ensure_inventory_name(connection)
            except sqlite3.OperationalError as exc:
                message = str(exc)
                if "no such table" in message.lower():
                    self._search_queue.put({"type": "no_inventory", "drive": drive_label})
                    return
                migration_error = message
                use_name = False
            except Exception as exc:
                migration_error = str(exc)
                use_name = False
            if cancel_event.is_set():
                return
            sql, params = build_search_query(query_parts["like"], use_name=use_name)
            cursor = connection.cursor()
            start = time.perf_counter()
            rows = cursor.execute(sql, params).fetchall()
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            formatted = format_results(rows)
            if cancel_event.is_set():
                return
            self._search_queue.put(
                {
                    "type": "results",
                    "results": formatted,
                    "elapsed_ms": elapsed_ms,
                    "query": query_parts["text"],
                    "drive": drive_label,
                    "fallback": not use_name,
                    "migration_error": migration_error,
                }
            )
        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "no such table" in message.lower():
                self._search_queue.put({"type": "no_inventory", "drive": drive_label})
            else:
                self._search_queue.put({"type": "error", "message": message, "drive": drive_label})
        except Exception as exc:
            self._search_queue.put({"type": "error", "message": str(exc), "drive": drive_label})
        finally:
            if connection is not None:
                connection.close()

    def _poll_search_queue(self):
        try:
            while True:
                payload = self._search_queue.get_nowait()
                kind = payload.get("type")
                if kind == "results":
                    self._apply_search_results(payload)
                elif kind == "error":
                    self._handle_search_error(payload.get("message") or "Unknown error")
                elif kind == "no_inventory":
                    self._handle_search_no_inventory()
        except queue.Empty:
            pass
        finally:
            if not self._closing:
                self.root.after(200, self._poll_search_queue)

    def _apply_search_results(self, payload: dict) -> None:
        self._search_thread = None
        self._search_cancel = None
        self._search_results = list(payload.get("results") or [])
        if hasattr(self, "search_tree"):
            self.search_tree.delete(*self.search_tree.get_children())
        self._search_rows.clear()
        for result in self._search_results:
            values = (
                result.get("name", ""),
                result.get("category", ""),
                result.get("size_display", ""),
                result.get("modified_display", ""),
                result.get("drive", ""),
                result.get("path", ""),
            )
            item = self.search_tree.insert("", END, values=values)
            self._search_rows[item] = result
        elapsed_ms = payload.get("elapsed_ms")
        total = len(self._search_results)
        if elapsed_ms is not None:
            status = f"Results: {total:,} (showing first 1,000) — elapsed {elapsed_ms} ms"
        else:
            status = f"Results: {total:,} (showing first 1,000)"
        if payload.get("fallback"):
            status += " — path-only search"
        self.search_status_var.set(status)
        migration_error = payload.get("migration_error")
        if migration_error:
            self.show_banner(
                f"Search index migration failed: {migration_error}. Using path-only search.",
                "ERROR",
            )

    def _handle_search_error(self, message: str) -> None:
        self._search_thread = None
        self._search_cancel = None
        self.search_status_var.set(f"Error: {message}")
        self.show_banner(f"Quick Search error: {message}", "ERROR")

    def _handle_search_no_inventory(self) -> None:
        self._search_thread = None
        self._search_cancel = None
        if hasattr(self, "search_tree"):
            self.search_tree.delete(*self.search_tree.get_children())
        self._search_rows.clear()
        self._search_results = []
        message = "No inventory found. Run Inventory first."
        self.search_status_var.set(message)
        self.show_banner(message, "ERROR")

    def _get_selected_search_result(self) -> Optional[dict]:
        if not hasattr(self, "search_tree"):
            return None
        selection = self.search_tree.selection()
        if not selection:
            return None
        return self._search_rows.get(selection[0])

    def _on_search_item_open(self, _event=None) -> None:
        self._open_selected_search_path()

    def _open_selected_search_path(self) -> None:
        result = self._get_selected_search_result()
        if not result:
            return
        path = result.get("path") or ""
        if path:
            self._open_path_in_explorer(path)

    def _show_search_menu(self, event) -> None:
        if not hasattr(self, "search_menu"):
            return
        try:
            row = self.search_tree.identify_row(event.y)
            if row:
                self.search_tree.selection_set(row)
            self.search_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.search_menu.grab_release()
            except Exception:
                pass

    def _copy_search_path(self) -> None:
        result = self._get_selected_search_result()
        if not result:
            return
        path = result.get("path") or ""
        if not path:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)
            self.search_status_var.set("Copied path to clipboard.")
        except Exception as exc:
            messagebox.showerror("Quick Search", f"Unable to copy path: {exc}")

    def _export_search_results(self) -> None:
        if not self._search_results:
            messagebox.showinfo("Quick Search", "No results to export.")
            return
        try:
            exported = export_results(self._search_results, EXPORTS_DIR_PATH)
        except Exception as exc:
            messagebox.showerror("Quick Search", f"Export failed: {exc}")
            return
        names = ", ".join(path.name for path in exported)
        summary = f"Exported {len(self._search_results):,} results to {names}"
        self.search_status_var.set(summary)
        self.show_banner(summary, "SUCCESS", action=("Open exports folder", self.open_exports_folder))
        self._recent_export_paths.extend(exported)

    def _open_path_in_explorer(self, path: str) -> None:
        try:
            if sys.platform.startswith("win"):
                normalized = os.path.normpath(path)
                if os.path.exists(normalized):
                    subprocess.Popen(["explorer", "/select,", normalized])
                else:
                    parent = os.path.dirname(normalized) or normalized
                    subprocess.Popen(["explorer", parent])
            elif sys.platform == "darwin":
                folder = os.path.dirname(path) or path
                subprocess.Popen(["open", folder])
            else:
                folder = os.path.dirname(path) or path
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            messagebox.showerror("Quick Search", f"Unable to open folder: {exc}")

    # ----- Search+ helpers -------------------------------------------------
    def start_search_plus(self) -> None:
        if not hasattr(self, "search_plus_query_widget"):
            return
        query = self.search_plus_query_widget.get("1.0", "end").strip()
        if len(query) < 3:
            message = "Type at least 3 characters."
            self.search_plus_status_var.set(message)
            self.show_banner(message, "INFO")
            return

        if self._search_plus_thread and self._search_plus_thread.is_alive():
            if self._search_plus_cancel:
                self._search_plus_cancel.set()

        self._search_plus_token += 1
        token = self._search_plus_token
        self._search_plus_cancel = threading.Event()
        mode = (self.search_plus_mode_var.get() or "semantic").strip().lower()
        type_filter = self.search_plus_type_var.get() or "Any"
        date_filter = self.search_plus_date_var.get() or "Any time"

        self._clear_search_plus_results()
        self._start_search_plus_progress()
        status_labels = {
            "semantic": "Semantic index",
            "transcripts": "Transcripts",
            "inventory": "Inventory",
        }
        for key, var in self._search_plus_service_vars.items():
            label = status_labels.get(key, key.title())
            var.set(f"{label} — queued…")

        self.search_plus_status_var.set("Searching…")
        try:
            self.search_plus_button.configure(state="disabled")
        except Exception:
            pass

        self._search_plus_thread = threading.Thread(
            target=self._search_plus_worker,
            args=(token, query, mode, type_filter, date_filter, self._search_plus_cancel),
            daemon=True,
        )
        self._search_plus_thread.start()

    def _clear_search_plus_results(self) -> None:
        self._search_plus_results = []
        self._search_plus_rows.clear()
        self._search_plus_images.clear()
        if hasattr(self, "search_plus_tree"):
            try:
                self.search_plus_tree.delete(*self.search_plus_tree.get_children())
            except Exception:
                pass
        self._on_search_plus_selection()

    def _search_plus_worker(
        self,
        token: int,
        query: str,
        mode: str,
        type_filter: str,
        date_filter: str,
        cancel_event: Optional[threading.Event],
    ) -> None:
        base_url, api_key = self._resolve_search_plus_endpoint()
        if not base_url:
            self._search_plus_queue.put(
                {
                    "type": "search_plus_error",
                    "token": token,
                    "message": "Configure the API host before using Search+.",
                }
            )
            return
        normalized_type = (type_filter or "").strip().lower()
        if normalized_type in {"", "any"}:
            normalized_type = ""
        since_iso = self._resolve_search_plus_since(date_filter)
        drives = list(self._search_drive_options)

        def _status_callback(service: str, state: str, meta: Dict[str, Any]) -> None:
            self._search_plus_queue.put(
                {
                    "type": "search_plus_status",
                    "token": token,
                    "service": service,
                    "state": state,
                    "meta": dict(meta or {}),
                }
            )

        client = SearchPlusClient(
            base_url,
            api_key=api_key,
            timeout=12.0,
        )
        try:
            response = client.search(
                query,
                mode=mode,
                type_filter=normalized_type or None,
                since=since_iso,
                drives=drives,
                limit=60,
                cancel_event=cancel_event,
                status_callback=_status_callback,
            )
        except Exception as exc:
            self._search_plus_queue.put(
                {
                    "type": "search_plus_error",
                    "token": token,
                    "message": str(exc) or exc.__class__.__name__,
                }
            )
            return

        if cancel_event and cancel_event.is_set():
            self._search_plus_queue.put({"type": "search_plus_cancelled", "token": token})
            return

        self._search_plus_queue.put(
            {
                "type": "search_plus_results",
                "token": token,
                "results": [asdict(result) for result in response.results],
                "durations": response.durations_ms,
                "errors": response.errors,
                "source_counts": response.source_counts,
            }
        )

    def _poll_search_plus_queue(self):
        try:
            while True:
                payload = self._search_plus_queue.get_nowait()
                token = payload.get("token")
                if token is not None and token != self._search_plus_token:
                    continue
                kind = payload.get("type")
                if kind == "search_plus_status":
                    self._update_search_plus_service_status(
                        payload.get("service"),
                        payload.get("state"),
                        payload.get("meta") or {},
                    )
                elif kind == "search_plus_results":
                    self._apply_search_plus_results(payload)
                elif kind == "search_plus_error":
                    self._handle_search_plus_error(payload.get("message") or "Unknown error")
                elif kind == "search_plus_cancelled":
                    self._finalize_search_plus(cancelled=True)
        except queue.Empty:
            pass
        finally:
            if not self._closing:
                self.root.after(200, self._poll_search_plus_queue)

    def _finalize_search_plus(self, *, cancelled: bool = False) -> None:
        self._stop_search_plus_progress()
        self._search_plus_thread = None
        self._search_plus_cancel = None
        try:
            self.search_plus_button.configure(state="normal")
        except Exception:
            pass
        if cancelled:
            self.search_plus_status_var.set("Search cancelled.")

    def _update_search_plus_service_status(
        self,
        service: Optional[str],
        state: Optional[str],
        meta: Dict[str, Any],
    ) -> None:
        if not service:
            return
        var = self._search_plus_service_vars.get(service)
        if not var:
            return
        label_map = {
            "semantic": "Semantic index",
            "transcripts": "Transcripts",
            "inventory": "Inventory",
        }
        label = label_map.get(service, service.title())
        normalized = (state or "").lower()
        if normalized == "running":
            text = f"{label} — running…"
        elif normalized == "done":
            count = meta.get("count")
            elapsed = meta.get("elapsed_ms")
            if elapsed is not None:
                text = f"{label} — {int(count or 0):,} hits ({int(elapsed)} ms)"
            else:
                text = f"{label} — {int(count or 0):,} hits"
        elif normalized == "error":
            message = str(meta.get("message") or "error")
            text = f"{label} — error: {message}"
        elif normalized == "cancelled":
            text = f"{label} — cancelled"
        elif normalized == "queued":
            text = f"{label} — queued…"
        else:
            text = f"{label} — {state or 'idle'}"
        var.set(text)

    def _apply_search_plus_results(self, payload: Dict[str, Any]) -> None:
        self._finalize_search_plus(cancelled=False)
        self._search_plus_results = list(payload.get("results") or [])
        durations = payload.get("durations") or {}
        errors = payload.get("errors") or {}
        counts = payload.get("source_counts") or {}

        if hasattr(self, "search_plus_tree"):
            try:
                self.search_plus_tree.delete(*self.search_plus_tree.get_children())
            except Exception:
                pass
        self._search_plus_rows.clear()
        self._search_plus_images.clear()

        for result in self._search_plus_results:
            thumbnail = self._decode_search_plus_thumbnail(result.get("thumbnail"))
            if thumbnail is not None:
                self._search_plus_images.append(thumbnail)
            elif self._search_plus_placeholder_image is not None:
                thumbnail = self._search_plus_placeholder_image
            sources = result.get("extras", {}).get("sources") or [result.get("source")]
            source_label = ", ".join(
                sorted({str(source).title() for source in sources if source})
            ) or "—"
            try:
                score_value = float(result.get("score", 0.0))
            except (TypeError, ValueError):
                score_value = 0.0
            score_text = f"{score_value:.3f}"
            title = result.get("title") or os.path.basename(result.get("path") or "")
            snippet = self._trim_snippet(result.get("snippet") or "")
            values = (
                title,
                score_text,
                result.get("drive") or "",
                source_label,
                snippet,
                result.get("path") or "",
            )
            image_param = thumbnail if thumbnail is not None else ""
            try:
                item = self.search_plus_tree.insert(
                    "",
                    END,
                    text="",
                    image=image_param,
                    values=values,
                )
            except Exception:
                item = self.search_plus_tree.insert("", END, text="", values=values)
            self._search_plus_rows[item] = result

        summary_parts = [f"Results: {len(self._search_plus_results):,}"]
        if durations:
            timings = ", ".join(f"{key} {int(value)} ms" for key, value in durations.items())
            summary_parts.append(f"timings: {timings}")
        if counts:
            breakdown = ", ".join(f"{key} {value}" for key, value in counts.items())
            summary_parts.append(f"sources: {breakdown}")
        if errors:
            issues = "; ".join(f"{key}: {msg}" for key, msg in errors.items())
            summary_parts.append(f"errors: {issues}")
            self.show_banner(f"Search+ warnings: {issues}", "WARNING")
        self.search_plus_status_var.set(" — ".join(summary_parts))
        self._on_search_plus_selection()

    def _handle_search_plus_error(self, message: str) -> None:
        self._finalize_search_plus(cancelled=False)
        self.search_plus_status_var.set(f"Error: {message}")
        self.show_banner(f"Search+ error: {message}", "ERROR")

    def _get_selected_search_plus_result(self) -> Optional[dict]:
        if not hasattr(self, "search_plus_tree"):
            return None
        selection = self.search_plus_tree.selection()
        if not selection:
            return None
        return self._search_plus_rows.get(selection[0])

    def _on_search_plus_item_open(self, _event=None) -> None:
        self._open_selected_search_plus_path()

    def _on_search_plus_selection(self, _event=None) -> None:
        result = self._get_selected_search_plus_result()
        has_path = bool(result and result.get("path"))
        has_transcript = bool(result and result.get("transcript_url"))
        has_inventory = bool(result and result.get("inventory_url"))
        state = "normal" if has_path else "disabled"
        try:
            self.search_plus_open_button.configure(state=state)
            self.search_plus_copy_button.configure(state=state)
            self.search_plus_transcript_button.configure(
                state="normal" if has_transcript else "disabled"
            )
            self.search_plus_inventory_button.configure(
                state="normal" if has_inventory else "disabled"
            )
        except Exception:
            pass

    def _open_selected_search_plus_path(self) -> None:
        result = self._get_selected_search_plus_result()
        if not result:
            return
        path = result.get("path") or ""
        if path:
            self._open_path_in_explorer(path)

    def _copy_search_plus_path(self) -> None:
        result = self._get_selected_search_plus_result()
        if not result:
            return
        path = result.get("path") or ""
        if not path:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)
            self.search_plus_status_var.set("Copied path to clipboard.")
        except Exception as exc:
            messagebox.showerror("Search+", f"Unable to copy path: {exc}")

    def _open_selected_search_plus_transcript(self) -> None:
        result = self._get_selected_search_plus_result()
        if not result:
            return
        url = result.get("transcript_url")
        if url:
            try:
                webbrowser.open(url)
            except Exception as exc:
                messagebox.showerror("Search+", f"Unable to open transcript: {exc}")

    def _open_selected_search_plus_inventory(self) -> None:
        result = self._get_selected_search_plus_result()
        if not result:
            return
        url = result.get("inventory_url")
        if url:
            try:
                webbrowser.open(url)
            except Exception as exc:
                messagebox.showerror("Search+", f"Unable to open inventory detail: {exc}")

    def _show_search_plus_menu(self, event) -> None:
        if not hasattr(self, "search_plus_menu"):
            return
        try:
            row = self.search_plus_tree.identify_row(event.y)
            if row:
                self.search_plus_tree.selection_set(row)
            self.search_plus_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.search_plus_menu.grab_release()
            except Exception:
                pass

    def _resolve_search_plus_endpoint(self) -> tuple[Optional[str], Optional[str]]:
        endpoint = self.api_endpoint
        if endpoint:
            return endpoint, self.api_key_value or None
        base = f"http://{self.api_default_host}:{self.api_default_port}"
        return base, self.api_key_value or None

    def _resolve_search_plus_since(self, value: str) -> Optional[str]:
        text = (value or "").strip().lower()
        if text.startswith("last 7"):
            days = 7
        elif text.startswith("last 30"):
            days = 30
        elif text.startswith("last 180"):
            days = 180
        elif text.startswith("last 365"):
            days = 365
        else:
            return None
        try:
            threshold = datetime.now().astimezone() - timedelta(days=days)
        except Exception:
            threshold = datetime.now() - timedelta(days=days)
        return threshold.isoformat(timespec="seconds")

    def _start_search_plus_progress(self) -> None:
        if not hasattr(self, "search_plus_progress"):
            return
        try:
            if not self._search_plus_progress_running:
                self.search_plus_progress.start(12)
                self._search_plus_progress_running = True
        except Exception:
            self._search_plus_progress_running = False

    def _stop_search_plus_progress(self) -> None:
        if not hasattr(self, "search_plus_progress"):
            return
        if not self._search_plus_progress_running:
            return
        try:
            self.search_plus_progress.stop()
        except Exception:
            pass
        self._search_plus_progress_running = False

    def _decode_search_plus_thumbnail(self, data: Optional[str]) -> Optional[PhotoImage]:
        if not data:
            return None
        try:
            text = str(data)
            if text.startswith("data:") and "," in text:
                text = text.split(",", 1)[1]
            binary = base64.b64decode(text)
            encoded = base64.b64encode(binary).decode("ascii")
            return PhotoImage(data=encoded)
        except Exception:
            return None

    @staticmethod
    def _trim_snippet(snippet: str, limit: int = 220) -> str:
        text = str(snippet or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _update_search_drive_options(self, labels: List[str]) -> None:
        unique = sorted({label for label in labels if label})
        self._search_drive_options = unique
        if not hasattr(self, "search_drive_combo"):
            return
        self.search_drive_combo["values"] = unique
        current = self.search_drive_var.get()
        if current in unique:
            return
        candidate = self._last_scan_label or self.label_var.get().strip()
        selected = None
        if current and current in unique:
            selected = current
        elif candidate and candidate in unique:
            selected = candidate
        elif unique:
            selected = unique[0]
        if selected:
            self.search_drive_var.set(selected)
        else:
            self.search_drive_var.set("")

    def _update_reports_drive_options(self, labels: List[str]) -> None:
        unique = sorted({label for label in labels if label})
        if not hasattr(self, "reports_drive_combo"):
            return
        self.reports_drive_combo["values"] = unique
        current = self.reports_drive_var.get()
        candidate = self._last_scan_label or self.label_var.get().strip()
        if current and current in unique:
            return
        selected = None
        if candidate and candidate in unique:
            selected = candidate
        elif unique:
            selected = unique[0]
        if selected:
            self.reports_drive_var.set(selected)
        else:
            self.reports_drive_var.set("")

    def _reset_report_views(self) -> None:
        self._report_sections = {}
        self._reports_bundle = None
        self._report_sort_state.clear()
        for key, tree in self.report_trees.items():
            try:
                tree.delete(*tree.get_children())
            except Exception:
                pass
            tree["columns"] = ()
            tree["displaycolumns"] = ()
            self._report_tree_rows[key] = {}

    def _set_report_headings(
        self, section_key: str, active_column: Optional[str] = None, ascending: bool = True
    ) -> None:
        section = self._report_sections.get(section_key)
        tree = self.report_trees.get(section_key)
        if not section or not tree:
            return
        for column in section.columns:
            text = column.heading
            if active_column == column.key:
                text = f"{column.heading} {'▲' if ascending else '▼'}"
            tree.heading(
                column.key,
                text=text,
                command=lambda c=column.key, key=section_key: self._sort_report_tree(key, c),
            )

    def _sort_report_tree(self, section_key: str, column_key: str) -> None:
        tree = self.report_trees.get(section_key)
        section = self._report_sections.get(section_key)
        if not tree or not section:
            return
        rows = list(tree.get_children(""))
        if not rows:
            return
        column_meta = next((col for col in section.columns if col.key == column_key), None)
        numeric = bool(column_meta and column_meta.numeric)
        raw_map = self._report_tree_rows.get(section_key, {})

        def key_func(item: str):
            raw = raw_map.get(item) or {}
            value = raw.get(column_key)
            if value is None:
                value = tree.set(item, column_key)
            if numeric:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return 0.0
            return str(value)

        ascending = not self._report_sort_state.get((section_key, column_key), False)
        rows.sort(key=key_func, reverse=not ascending)
        for index, item in enumerate(rows):
            tree.move(item, "", index)
        self._report_sort_state[(section_key, column_key)] = ascending
        self._set_report_headings(section_key, column_key, ascending)

    def run_reports(self) -> None:
        drive_label = (self.reports_drive_var.get() or "").strip()
        if not drive_label:
            message = "Select a drive to run reports."
            self.reports_status_var.set(message)
            self.show_banner(message, "INFO")
            return
        shard = shard_path_for(drive_label)
        if not shard.exists():
            message = "No inventory available for this drive."
            self._reset_report_views()
            self.reports_status_var.set(message)
            self.show_banner(message, "ERROR")
            return
        try:
            top_n = max(5, int(self.reports_topn_var.get()))
        except (TypeError, ValueError):
            top_n = 20
            self.reports_topn_var.set(top_n)
        try:
            depth = max(1, int(self.reports_depth_var.get()))
        except (TypeError, ValueError):
            depth = 2
            self.reports_depth_var.set(depth)
        try:
            days = max(1, int(self.reports_days_var.get()))
        except (TypeError, ValueError):
            days = 30
            self.reports_days_var.set(days)
        largest_limit = max(100, top_n)
        if self._reports_thread and self._reports_thread.is_alive():
            if self._reports_cancel:
                self._reports_cancel.set()
        catalog_path = Path(self.db_path.get()) if self.db_path.get() else None
        self._reports_cancel = threading.Event()
        self._reports_run_token += 1
        token = self._reports_run_token
        worker_args = (
            token,
            shard,
            catalog_path,
            drive_label,
            top_n,
            depth,
            days,
            largest_limit,
            self._reports_cancel,
        )
        self._reports_thread = threading.Thread(
            target=self._reports_worker,
            args=worker_args,
            daemon=True,
        )
        self._reports_thread.start()
        self._reset_report_views()
        self.reports_status_var.set(f"Running reports for {drive_label}…")
        try:
            self.main_notebook.select(self.reports_tab)
        except Exception:
            pass

    def _reports_worker(
        self,
        token: int,
        shard_path: Path,
        catalog_path: Optional[Path],
        drive_label: str,
        top_n: int,
        depth: int,
        days: int,
        largest_limit: int,
        cancel_event: Optional[threading.Event],
    ) -> None:
        try:
            bundle = reports_util.generate_report(
                shard_path,
                catalog_path,
                drive_label,
                top_n=top_n,
                folder_depth=depth,
                recent_days=days,
                largest_limit=largest_limit,
                cancel_event=cancel_event,
            )
        except reports_util.ReportCancelled:
            return
        except reports_util.MissingInventoryError:
            self._reports_queue.put({"type": "no_inventory", "token": token, "drive": drive_label})
        except Exception as exc:
            self._reports_queue.put({"type": "error", "token": token, "message": str(exc)})
        else:
            self._reports_queue.put({"type": "results", "token": token, "bundle": bundle})

    def _poll_reports_queue(self):
        try:
            while True:
                payload = self._reports_queue.get_nowait()
                token = payload.get("token")
                if token is not None and token != self._reports_run_token:
                    continue
                kind = payload.get("type")
                if kind == "results":
                    bundle = payload.get("bundle")
                    if isinstance(bundle, reports_util.ReportBundle):
                        self._apply_reports_results(bundle)
                elif kind == "error":
                    self._handle_reports_error(payload.get("message") or "Unknown error")
                elif kind == "no_inventory":
                    self._handle_reports_no_inventory()
        except queue.Empty:
            pass
        finally:
            if not self._closing:
                self.root.after(200, self._poll_reports_queue)

    def _apply_reports_results(self, bundle: reports_util.ReportBundle) -> None:
        self._reports_thread = None
        self._reports_cancel = None
        self._reports_bundle = bundle
        sections = reports_util.bundle_to_sections(bundle)
        self._report_sections = sections
        total_rows = 0
        for key, section in sections.items():
            tree = self.report_trees.get(key)
            if not tree:
                continue
            self._report_tree_rows[key] = {}
            columns = [column.key for column in section.columns]
            tree["columns"] = columns
            tree["displaycolumns"] = columns
            for column in section.columns:
                anchor = column.anchor.lower() if column.anchor else "w"
                if anchor == "e":
                    anchor_value = E
                elif anchor == "center":
                    anchor_value = "center"
                else:
                    anchor_value = W
                width = column.width if column.width is not None else 160
                tree.column(
                    column.key,
                    anchor=anchor_value,
                    stretch=bool(column.stretch),
                    width=width,
                )
            self._set_report_headings(key)
            for idx, row in enumerate(section.rows):
                values = [row.get(column.key, "") for column in section.columns]
                item = tree.insert("", END, values=values)
                export_rows = section.export_rows
                if idx < len(export_rows):
                    self._report_tree_rows[key][item] = export_rows[idx]
                else:
                    self._report_tree_rows[key][item] = {}
            total_rows += len(section.rows)
        recent_total = bundle.recent_changes.total
        recent_shown = len(bundle.recent_changes.rows)
        status = f"Report generated in {bundle.elapsed_ms} ms — results: {total_rows:,} rows"
        if recent_total:
            if recent_total > recent_shown:
                status += f" — recent files: {recent_total:,} (showing {recent_shown:,})"
            else:
                status += f" — recent files: {recent_total:,}"
        source = bundle.overview.source
        if source and source != "inventory":
            status += f" — categories from {source.replace('_', ' ')}"
        self.reports_status_var.set(status)

    def _handle_reports_error(self, message: str) -> None:
        self._reports_thread = None
        self._reports_cancel = None
        self.reports_status_var.set(f"Error: {message}")
        self.show_banner(f"Reports error: {message}", "ERROR")

    def _handle_reports_no_inventory(self) -> None:
        self._reports_thread = None
        self._reports_cancel = None
        self._reset_report_views()
        message = "No inventory available for this drive."
        self.reports_status_var.set(message)
        self.show_banner(message, "ERROR")

    def export_reports(self, fmt: str) -> None:
        bundle = self._reports_bundle
        if not bundle:
            messagebox.showinfo("Reports", "Run the reports before exporting.")
            return
        export_dir = EXPORTS_DIR_PATH / "reports"
        try:
            if fmt == "csv":
                paths = reports_util.export_bundle_to_csv(bundle, export_dir)
                summary = f"CSV exports saved: {', '.join(path.name for path in paths)}"
                self.reports_status_var.set(summary)
                self.show_banner(
                    summary,
                    "SUCCESS",
                    action=("Open exports folder", self.open_exports_folder),
                )
                self._recent_export_paths.extend(paths)
            elif fmt == "json":
                path = reports_util.export_bundle_to_json(bundle, export_dir)
                summary = f"JSON export saved: {path.name}"
                self.reports_status_var.set(summary)
                self.show_banner(
                    summary,
                    "SUCCESS",
                    action=("Open exports folder", self.open_exports_folder),
                )
                self._recent_export_paths.append(path)
            else:
                raise ValueError("Unsupported export format")
            if len(self._recent_export_paths) > 5:
                self._recent_export_paths = self._recent_export_paths[-5:]
        except Exception as exc:
            messagebox.showerror("Reports", f"Export failed: {exc}")
            self.reports_status_var.set(f"Export failed: {exc}")

    def _set_status(self, message: str, level: str = "info"):
        if not hasattr(self, "status_var"):
            self.status_var = StringVar()
        self.status_var.set(message)
        style = self.status_styles.get(level, "StatusInfo.TLabel")
        if hasattr(self, "status_label"):
            self.status_label.configure(style=style)

    def _notify_maintenance_scan_state(self) -> None:
        if self._maintenance_window and self._maintenance_window.winfo_exists():
            label = self._last_scan_label or self.label_var.get().strip()
            self._maintenance_window.on_scan_state_change(self.scan_in_progress, label)

    def _trigger_auto_maintenance(self, label: Optional[str]) -> None:
        if not label:
            return
        shard_path = shard_path_for(label)
        if not shard_path.exists():
            return

        def worker():
            settings = load_settings(WORKING_DIR_PATH)
            options = resolve_options(settings)
            safe = safe_label(label)
            try:
                log(f"[Maintenance] Auto optimize for shard {label} starting.")
                quick_optimize(shard_path, busy_timeout_ms=options.busy_timeout_ms)
                if options.auto_vacuum_after_scan:
                    backup = light_backup(
                        shard_path,
                        f"auto_{safe}",
                        working_dir=WORKING_DIR_PATH,
                        busy_timeout_ms=options.busy_timeout_ms,
                    )
                    log(f"[Maintenance] Auto vacuum backup stored at {backup}")
                    vacuum_result = vacuum_if_needed(
                        shard_path,
                        thresholds={"vacuum_free_bytes_min": options.vacuum_free_bytes_min},
                        busy_timeout_ms=options.busy_timeout_ms,
                        force=False,
                        active_check=lambda: self.scan_in_progress,
                    )
                    if vacuum_result.get("skipped"):
                        reason = vacuum_result.get("reason") or "threshold"
                        log(f"[Maintenance] Auto vacuum skipped for {label}: {reason}")
                    else:
                        reclaimed = int(vacuum_result.get("reclaimed_bytes") or 0)
                        log(
                            f"[Maintenance] Auto vacuum reclaimed {format_bytes(reclaimed)} for {label}"
                        )
                update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
            except Exception as exc:
                log(f"[Maintenance] Auto optimize failed for {label}: {exc}")
            finally:
                if self._maintenance_window and self._maintenance_window.winfo_exists():
                    try:
                        self.root.after(0, self._maintenance_window.refresh_targets)
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _update_performance_display(self, payload: dict):
        if not hasattr(self, "performance_var"):
            return
        profile = str(payload.get("profile") or "—")
        label = f"Performance: {profile}"
        extras: list[str] = []
        threads = payload.get("worker_threads")
        if isinstance(threads, int) and threads > 0:
            extras.append(f"threads={threads}")
        chunk = payload.get("hash_chunk_bytes")
        if isinstance(chunk, int) and chunk > 0:
            if chunk >= 1_048_576:
                chunk_str = f"{chunk / 1_048_576:.0f}MB"
            else:
                chunk_str = f"{max(1, chunk // 1024)}KB"
            extras.append(f"chunk={chunk_str}")
        ffmpeg = payload.get("ffmpeg_parallel")
        if isinstance(ffmpeg, int) and ffmpeg > 0:
            extras.append(f"ffmpeg={ffmpeg}")
        if extras:
            label += " (" + ", ".join(extras) + ")"
        self.performance_var.set(label)

    def _show_toast(self, message: str, level: str = "success", duration: int = 4000):
        toast = Toplevel(self.root)
        toast.withdraw()
        toast.overrideredirect(True)
        toast.transient(self.root)
        toast.attributes("-topmost", True)
        frame = ttk.Frame(toast, padding=(14, 10), style="Card.TFrame")
        frame.grid(row=0, column=0, sticky="nsew")
        toast.columnconfigure(0, weight=1)
        toast.rowconfigure(0, weight=1)
        color_map = {
            "success": self.colors.get("success"),
            "info": self.colors.get("info"),
            "error": self.colors.get("danger"),
        }
        fg = color_map.get(level, self.colors.get("accent"))
        label = ttk.Label(frame, text=message, style="TLabel")
        if fg:
            label.configure(foreground=fg)
        label.grid(row=0, column=0, sticky="w")
        toast.update_idletasks()
        width = toast.winfo_width() or frame.winfo_reqwidth()
        height = toast.winfo_height() or frame.winfo_reqheight()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        x = root_x + root_w - width - 40
        y = root_y + root_h - height - 60
        toast.geometry(f"{width}x{height}+{max(x, root_x + 20)}+{max(y, root_y + 20)}")
        toast.deiconify()
        self._active_toasts.append(toast)

        def _close(event=None):
            try:
                self._active_toasts.remove(toast)
            except ValueError:
                pass
            try:
                toast.destroy()
            except Exception:
                pass

        toast.after(duration, _close)
        toast.bind("<Button-1>", _close)

    def show_banner(
        self,
        message: str,
        level: str = "INFO",
        action: Optional[tuple[str, Callable[[], None]]] = None,
    ):
        self._tool_banner_active = False
        level_key = (level or "INFO").upper()
        styles = self.banner_styles.get(level_key, self.banner_styles["INFO"])
        if self.banner_after_id:
            self.root.after_cancel(self.banner_after_id)
            self.banner_after_id = None
        self.banner_message.set(message)
        self.banner_frame.configure(style=styles["frame"])
        self.banner_label.configure(style=styles["label"])
        if action:
            text, callback = action
            self.banner_action.configure(text=text, command=callback)
            self.banner_action.grid()
            self._banner_action_callback = callback
        else:
            self.banner_action.grid_remove()
            self._banner_action_callback = None
        if not self._banner_visible:
            self.banner_frame.grid()
            self._banner_visible = True
        if level_key == "SUCCESS":
            self.banner_after_id = self.root.after(8000, self.clear_banner)

    def _open_inventory_summary_dialog(self, total_files: int, totals: Dict[str, int], event: dict) -> None:
        dialog = Toplevel(self.root)
        dialog.title("Inventory summary")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=(16, 12))
        frame.grid(row=0, column=0, sticky="nsew")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        ttk.Label(frame, text=f"Total files: {total_files:,}", style="Heading.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        categories = [
            ("Video", "video"),
            ("Audio", "audio"),
            ("Image", "image"),
            ("Document", "document"),
            ("Archive", "archive"),
            ("Executable", "executable"),
            ("Other", "other"),
        ]
        for idx, (label, key) in enumerate(categories, start=1):
            value = int(totals.get(key, 0) or 0)
            ttk.Label(frame, text=f"{label}:").grid(row=idx, column=0, sticky="w", pady=2)
            ttk.Label(frame, text=f"{value:,}").grid(row=idx, column=1, sticky="e", pady=2)
        skipped_perm = int(event.get("skipped_perm") or 0)
        skipped_long = int(event.get("skipped_toolong") or 0)
        skipped_ignored = int(event.get("skipped_ignored") or 0)
        skip_row = len(categories) + 1
        ttk.Separator(frame).grid(row=skip_row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(frame, text="Skipped (permissions):").grid(row=skip_row + 1, column=0, sticky="w")
        ttk.Label(frame, text=f"{skipped_perm:,}").grid(row=skip_row + 1, column=1, sticky="e")
        ttk.Label(frame, text="Skipped (path too long):").grid(row=skip_row + 2, column=0, sticky="w")
        ttk.Label(frame, text=f"{skipped_long:,}").grid(row=skip_row + 2, column=1, sticky="e")
        ttk.Label(frame, text="Skipped (ignored patterns):").grid(row=skip_row + 3, column=0, sticky="w")
        ttk.Label(frame, text=f"{skipped_ignored:,}").grid(row=skip_row + 3, column=1, sticky="e")
        ttk.Button(frame, text="Close", command=dialog.destroy).grid(
            row=skip_row + 4, column=0, columnspan=2, pady=(12, 0)
        )
        dialog.geometry("320x320")

    def clear_banner(self):
        if self.banner_after_id:
            self.root.after_cancel(self.banner_after_id)
            self.banner_after_id = None
        if self._banner_visible:
            self.banner_frame.grid_remove()
            self._banner_visible = False
        self.banner_message.set("")
        self.banner_action.grid_remove()
        self._banner_action_callback = None
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

    # ----- Local API helpers
    def _format_api_status(self, running: bool, extra: Optional[str] = None) -> str:
        key_text = "key set" if self.api_key_present else "key missing"
        base = f"host {self.api_default_host}:{self.api_default_port} — API {key_text}"
        if running:
            endpoint = extra or f"http://{self.api_default_host}:{self.api_default_port}"
            return f"Running — {endpoint} — {base}"
        if extra:
            return f"{extra} — {base}"
        return f"Stopped — {base}"

    def toggle_local_api(self):
        process = getattr(self, "api_process", None)
        if process and process.poll() is None:
            self.stop_local_api()
        else:
            self.start_local_api()

    def start_local_api(self):
        if getattr(self, "api_process", None) and self.api_process.poll() is None:
            return
        if not API_SCRIPT_PATH.exists():
            messagebox.showerror(
                "Local API",
                "videocatalog_api.py is missing. Ensure the CLI script is present before starting the API.",
            )
            return
        cmd = [
            sys.executable,
            str(API_SCRIPT_PATH),
            "--host",
            str(self.api_default_host),
            "--port",
            str(self.api_default_port),
        ]
        self.api_stopping = False
        self.api_endpoint = None
        self.api_status_var.set(self._format_api_status(False, "Starting…"))
        self.api_toggle_button.configure(state="disabled")
        try:
            self.api_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self.api_process = None
            self.api_toggle_button.configure(state="normal")
            self.api_status_var.set(self._format_api_status(False, "Start failed"))
            messagebox.showerror("Local API", f"Could not start the local API: {exc}")
            return
        self.api_toggle_button.configure(text="Stop Local API", state="normal")
        self.api_monitor_thread = threading.Thread(
            target=self._monitor_api_process,
            args=(self.api_process,),
            daemon=True,
        )
        self.api_monitor_thread.start()

    def _monitor_api_process(self, process: subprocess.Popen):
        endpoint_reported = False
        try:
            if process.stdout:
                for line in process.stdout:
                    text = line.strip()
                    if not text:
                        continue
                    log(f"[api] {text}")
                    if not endpoint_reported and text.lower().startswith("api listening on"):
                        endpoint_reported = True
                        endpoint = text.split("API listening on", 1)[-1].strip()
                        self.root.after(0, lambda e=endpoint: self._set_api_running(True, e))
            return_code = process.wait()
        except Exception as exc:
            log(f"[api] monitor error: {exc}")
            return_code = -1
        finally:
            self.root.after(0, lambda code=return_code: self._handle_api_exit(code))

    def _set_api_running(self, running: bool, endpoint: Optional[str] = None):
        self.api_running = running
        if running:
            self.api_endpoint = endpoint
            self.api_toggle_button.configure(text="Stop Local API", state="normal")
            self.api_status_var.set(self._format_api_status(True, endpoint))
        else:
            self.api_toggle_button.configure(text="Start Local API", state="normal")
            self.api_status_var.set(self._format_api_status(False))

    def _handle_api_exit(self, return_code: int):
        process = getattr(self, "api_process", None)
        if process and process.poll() is None:
            return
        self.api_process = None
        self.api_monitor_thread = None
        if self.api_stopping:
            self.api_stopping = False
            self._set_api_running(False)
            return
        if self.api_running:
            self.api_running = False
        self.api_toggle_button.configure(text="Start Local API", state="normal")
        status_text = "Stopped"
        if return_code not in (0, None):
            status_text = f"Stopped (exit {return_code})"
            self.show_banner("Local API stopped unexpectedly.", "WARNING")
        self.api_status_var.set(self._format_api_status(False, status_text))

    def stop_local_api(self):
        process = getattr(self, "api_process", None)
        if not process:
            self._set_api_running(False)
            return
        self.api_stopping = True
        self.api_status_var.set(self._format_api_status(False, "Stopping…"))
        self.api_toggle_button.configure(state="disabled")
        try:
            process.terminate()
        except Exception:
            pass

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
        if self.scan_in_progress:
            messagebox.showwarning("VACUUM", "Cannot run VACUUM while a scan is active.")
            return
        options = resolve_options(load_settings(WORKING_DIR_PATH))
        db_path = Path(self.db_path.get())
        try:
            backup = light_backup(
                db_path,
                "catalog_manual",
                working_dir=WORKING_DIR_PATH,
                busy_timeout_ms=options.busy_timeout_ms,
            )
            result = vacuum_if_needed(
                db_path,
                thresholds={"vacuum_free_bytes_min": options.vacuum_free_bytes_min},
                busy_timeout_ms=options.busy_timeout_ms,
                force=True,
                active_check=lambda: self.scan_in_progress,
            )
            update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
        except Exception as exc:
            messagebox.showerror("VACUUM", f"VACUUM failed:\n{exc}")
            return
        reclaimed = int(result.get("reclaimed_bytes") or 0)
        messagebox.showinfo(
            "VACUUM",
            f"Backup stored at:\n{backup}\n\nReclaimed: {format_bytes(reclaimed)}",
        )
        self._set_status("Catalog VACUUM completed.", "success")
        if self._maintenance_window and self._maintenance_window.winfo_exists():
            self._maintenance_window.refresh_targets()

    # ----- Buttons
    def choose_path(self):
        d = filedialog.askdirectory()
        if d: self.path_var.set(d)


    def export_db(self):
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dst = Path(EXPORTS_DIR) / f"catalog_{ts}.db"
        shutil.copy2(self.db_path.get(), dst)
        messagebox.showinfo("Export", f"Exported to\n{dst}")

    def open_exports_folder(self):
        exports_dir = EXPORTS_DIR_PATH
        try:
            exports_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Exports", f"Could not prepare exports folder\n{exc}")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(exports_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(exports_dir)])
            else:
                subprocess.Popen(["xdg-open", str(exports_dir)])
        except Exception as exc:
            messagebox.showerror("Exports", f"Unable to open exports folder\n{exc}")

    def _resolve_export_drive(self) -> Optional[str]:
        drive_label: Optional[str] = None
        if hasattr(self, "tree"):
            selection = self.tree.selection()
            if selection:
                values = self.tree.item(selection[0]).get("values", [])
                if isinstance(values, (list, tuple)) and len(values) > 1:
                    drive_value = values[1]
                    if drive_value:
                        drive_label = str(drive_value)
        if not drive_label:
            candidate = self.label_var.get().strip() if hasattr(self, "label_var") else ""
            if candidate:
                drive_label = candidate
        return drive_label

    def request_export(self, fmt: str):
        fmt_normalized = (fmt or "").strip().lower()
        if fmt_normalized not in {"csv", "jsonl"}:
            messagebox.showerror("Exports", "Unsupported export format.")
            return
        drive_label = self._resolve_export_drive()
        if not drive_label:
            messagebox.showinfo("Exports", "Select a drive row first.")
            return
        shard_path = shard_path_for(drive_label)
        if not shard_path.exists():
            messagebox.showwarning(
                "Exports",
                f"No shard database found for '{drive_label}'. Run a scan before exporting.",
            )
            return

        dialog = Toplevel(self.root)
        dialog.title(f"Export {drive_label} → {fmt_normalized.upper()}")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(padx=18, pady=16)
        dialog.grab_set()

        include_var = IntVar(value=0)
        av_only_var = IntVar(value=0)

        ttk.Label(
            dialog,
            text=f"Drive: {drive_label}\nFormat: {fmt_normalized.upper()}",
            style="HeaderSubtitle.TLabel",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Checkbutton(
            dialog,
            text="Include deleted",
            variable=include_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ttk.Checkbutton(
            dialog,
            text="AV only",
            variable=av_only_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 12))

        ttk.Label(
            dialog,
            text=f"Exports are saved under:\n{EXPORTS_DIR_PATH}",
            style="Subtle.TLabel",
            justify="left",
            wraplength=360,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 12))

        buttons = ttk.Frame(dialog, style="Card.TFrame")
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

        def _close(start: bool = False):
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()
            if start:
                self._start_export(
                    drive_label,
                    shard_path,
                    fmt_normalized,
                    include_deleted=bool(include_var.get()),
                    av_only=bool(av_only_var.get()),
                )

        ttk.Button(buttons, text="Cancel", command=lambda: _close(False)).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(
            buttons,
            text="Start export",
            style="Accent.TButton",
            command=lambda: _close(True),
        ).grid(row=0, column=1, sticky="ew")

        def _on_close():
            _close(False)

        dialog.protocol("WM_DELETE_WINDOW", _on_close)
        dialog.update_idletasks()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        xpos = root_x + (root_w - width) // 2
        ypos = root_y + (root_h - height) // 3
        dialog.geometry(f"{width}x{height}+{max(xpos, root_x + 20)}+{max(ypos, root_y + 40)}")
        dialog.wait_window()

    def _start_export(
        self,
        drive_label: str,
        shard_path: Path,
        fmt: str,
        include_deleted: bool,
        av_only: bool,
    ):
        fmt_lower = fmt.lower()
        fmt_upper = fmt_lower.upper()
        if not shard_path.exists():
            messagebox.showwarning(
                "Exports",
                f"Shard database missing for '{drive_label}'.",
            )
            return
        for worker in list(self._export_workers.values()):
            if worker.is_alive() and worker.drive_label == drive_label and worker.format == fmt_lower:
                messagebox.showinfo(
                    "Exports",
                    f"An {fmt_upper} export is already running for '{drive_label}'.",
                )
                return
        export_worker = CatalogExportWorker(
            drive_label=drive_label,
            shard_path=shard_path,
            fmt=fmt_lower,
            include_deleted=include_deleted,
            av_only=av_only,
            event_queue=self.worker_queue,
        )
        self._export_workers[export_worker.token] = export_worker
        export_worker.start()
        status_text = f"Exporting {drive_label} → {fmt_upper}…"
        self._set_status(status_text, "accent")
        if hasattr(self, "status_line_var"):
            self.status_line_label.configure(style=self.status_line_active_style)
            self.status_line_var.set(status_text)
            self.status_line_idle_text = status_text
            self._update_status_line(force=True)
        self._show_toast(f"{fmt_upper} export started for {drive_label}", "info", duration=3500)

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
        self._recent_export_paths = []
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
            light_analysis=bool(self.light_analysis_var.get()),
            inventory_only=bool(self.inventory_var.get()),
            gpu_policy=self.gpu_policy_var.get(),
            gpu_hwaccel=bool(self.gpu_hwaccel_var.get()),
            max_workers=threads,
            full_rescan=self.rescan_mode_var.get().lower() == "full",
            resume_enabled=bool(self.resume_var.get()),
            checkpoint_seconds=5,
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
        self._last_scan_label = self.label_var.get().strip()
        self.scan_in_progress = True
        self.scan_start_ts = time.time()
        self.last_progress_ts = self.scan_start_ts
        self.skipped_detail_var.set("Skipped: perm=0, long=0, ignored=0")
        self.progress_snapshot = {
            "phase": "enumerating",
            "files_total": None,
            "files_seen": 0,
            "av_seen": 0,
            "elapsed_s": 0,
            "total_av": None,
            "skipped_perm": 0,
            "skipped_toolong": 0,
            "skipped_ignored": 0,
        }
        self.heartbeat_active = False
        self.status_line_idle_text = "Ready."
        if hasattr(self, "performance_var"):
            self.performance_var.set("Performance: detecting…")
        self._start_activity_indicator()
        self._update_status_line(force=True)
        self.worker.start()
        self.root.after(800, self.refresh_jobs)
        self._notify_maintenance_scan_state()

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
            skipped_perm = event.get("skipped_perm")
            skipped_long = event.get("skipped_toolong")
            skipped_ignored = event.get("skipped_ignored")
            if skipped_perm is None and self.progress_snapshot:
                skipped_perm = self.progress_snapshot.get("skipped_perm")
            if skipped_long is None and self.progress_snapshot:
                skipped_long = self.progress_snapshot.get("skipped_toolong")
            if skipped_ignored is None and self.progress_snapshot:
                skipped_ignored = self.progress_snapshot.get("skipped_ignored")
            skipped_perm = int(skipped_perm or 0)
            skipped_long = int(skipped_long or 0)
            skipped_ignored = int(skipped_ignored or 0)
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
            self.skipped_detail_var.set(
                f"Skipped: perm={skipped_perm:,}, long={skipped_long:,}, ignored={skipped_ignored:,}"
            )
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
                "skipped_perm": skipped_perm,
                "skipped_toolong": skipped_long,
                "skipped_ignored": skipped_ignored,
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
        elif etype == "performance":
            self._update_performance_display(event)
        elif etype == "tool_missing":
            tool = str(event.get("tool", "")).strip() or "tool"
            self.scan_in_progress = False
            self._notify_maintenance_scan_state()
            self._stop_activity_indicator()
            message = f"Error — {tool} not found. Please install {tool} and try again."
            self.show_banner(message, "ERROR")
            self._set_status("Scan error.", "error")
            self.progress_detail_var.set("Scan stopped due to missing tool.")
            self.status_line_idle_text = message
            self.progress_snapshot = None
            self.last_progress_ts = None
            self._update_status_line(force=True)
        elif etype == "light_analysis_status":
            la_status = str(event.get("status") or "").lower()
            message = event.get("message") or "Light analysis update."
            if la_status == "error":
                self.show_banner(message, "ERROR")
                self._set_status(message, "error")
            elif la_status == "warning":
                self.show_banner(message, "WARNING")
                self._set_status(message, "warning")
            else:
                self._set_status(message, "info")
        elif etype == "refresh_jobs":
            self.refresh_jobs()
        elif etype == "error":
            messagebox.showerror(event.get("title", "Error"), event.get("message", ""))
        elif etype == "log":
            self._append_log(event.get("line", ""))
        elif etype == "export_progress":
            fmt = str(event.get("format", "")).upper()
            rows = int(event.get("rows", 0) or 0)
            drive_label = event.get("drive_label") or ""
            self._set_status(f"Exporting {drive_label} → {fmt} ({rows:,} rows)", "accent")
            self.status_line_var.set(f"Exporting {drive_label} → {fmt} ({rows:,} rows)")
            self._update_status_line(force=True)
        elif etype == "export_done":
            fmt = str(event.get("format", "")).upper()
            drive_label = event.get("drive_label") or ""
            token = event.get("token")
            if token:
                self._export_workers.pop(token, None)
            path_value = event.get("path")
            export_path = Path(path_value) if path_value else None
            if export_path:
                self._recent_export_paths.append(export_path)
                if len(self._recent_export_paths) > 5:
                    self._recent_export_paths = self._recent_export_paths[-5:]
            count = int(event.get("count", 0) or 0)
            status_text = f"{fmt} export finished ({count:,} rows)"
            self._set_status(status_text, "success")
            self.status_line_var.set(status_text)
            if not self.scan_in_progress:
                self.status_line_idle_text = status_text
            self._update_status_line(force=True)
            message = f"{fmt} export saved:\n{export_path}" if export_path else f"{fmt} export finished"
            self._show_toast(message, "success")
        elif etype == "export_error":
            fmt = str(event.get("format", "")).upper()
            message = event.get("message") or "Export failed."
            token = event.get("token")
            if token:
                self._export_workers.pop(token, None)
            self._set_status(f"{fmt} export failed", "error")
            self.show_banner(f"{fmt} export failed — {message}", "ERROR")
            if not self.scan_in_progress:
                self.status_line_idle_text = f"{fmt} export failed."
            self.status_line_var.set(f"{fmt} export failed")
            self._update_status_line(force=True)
        elif etype == "done":
            self._await_worker_completion()
            status = event.get("status", "Ready")
            self.scan_in_progress = False
            self._notify_maintenance_scan_state()
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
                inventory_totals = event.get("inventory_totals")
                fp_info = event.get("fingerprints")
                fp_line = None
                fp_level: Optional[str] = None
                if isinstance(fp_info, dict):
                    fp_status = str(fp_info.get("status") or "").lower()
                    if fp_status == "ok":
                        fp_line = (
                            "Fingerprints — video: "
                            f"{int(fp_info.get('video', 0)):,}, audio: {int(fp_info.get('audio', 0)):,}, "
                            f"prefilter: {int(fp_info.get('vhash', 0)):,}"
                        )
                        if fp_info.get("errors"):
                            fp_line += " (warnings: " + ", ".join(
                                str(err) for err in fp_info.get("errors", [])
                            ) + ")"
                            fp_level = "WARNING"
                    elif fp_info.get("errors"):
                        fp_line = "Fingerprints skipped — " + "; ".join(
                            str(err) for err in fp_info.get("errors", [])
                        )
                        fp_level = "ERROR"
                    elif fp_status == "skipped":
                        fp_line = "Fingerprints skipped — disabled"
                if isinstance(inventory_totals, dict):
                    summary_text = event.get("message") or (
                        "Inventory done — total files: "
                        f"{(total_all or 0):,} — duration: {self._format_elapsed(duration)}"
                    )
                    light_info = event.get("light_analysis")
                    light_line = None
                    if isinstance(light_info, dict):
                        la_status = str(light_info.get("status") or "").lower()
                        if la_status == "ok":
                            light_line = (
                                "Light analysis — images: "
                                f"{int(light_info.get('images', 0)):,}, videos: {int(light_info.get('videos', 0)):,}, "
                                f"avg dim: {int(light_info.get('avg_dim', 0))}"
                            )
                        if light_info.get("warning"):
                            light_line += f" (warning: {light_info.get('warning')})"
                    elif la_status in {"error", "skipped"} and light_info.get("message"):
                        light_line = f"Light analysis skipped — {light_info.get('message')}"
                    if light_line:
                        summary_text = f"{summary_text} • {light_line}"
                    if fp_line:
                        summary_text = f"{summary_text} • {fp_line}"
                    banner_level = "SUCCESS"
                    if fp_level == "ERROR":
                        banner_level = "ERROR"
                    elif fp_level == "WARNING":
                        banner_level = "WARNING"
                    self.status_line_idle_text = summary_text
                    action = (
                        "View summary",
                        lambda totals=inventory_totals, snapshot=event: self._open_inventory_summary_dialog(
                            total_all or 0,
                            totals,
                            snapshot,
                        ),
                    )
                    self.show_banner(summary_text, banner_level, action=action)
                else:
                    summary_text = (
                        "Done — total files: "
                        f"{(total_all or 0):,} — AV files: {(total_av or 0):,} — duration: {self._format_elapsed(duration)}"
                    )
                    light_info = event.get("light_analysis")
                    light_line = None
                    if isinstance(light_info, dict):
                        la_status = str(light_info.get("status") or "").lower()
                        if la_status == "ok":
                            light_line = (
                                "Light analysis — images: "
                                f"{int(light_info.get('images', 0)):,}, videos: {int(light_info.get('videos', 0)):,}, "
                                f"avg dim: {int(light_info.get('avg_dim', 0))}"
                            )
                        if light_info.get("warning"):
                            light_line += f" (warning: {light_info.get('warning')})"
                    elif la_status in {"error", "skipped"} and light_info.get("message"):
                        light_line = f"Light analysis skipped — {light_info.get('message')}"
                    if light_line:
                        summary_text = f"{summary_text} • {light_line}"
                    if fp_line:
                        summary_text = f"{summary_text} • {fp_line}"
                    banner_level = "SUCCESS"
                    if fp_level == "ERROR":
                        banner_level = "ERROR"
                    elif fp_level == "WARNING":
                        banner_level = "WARNING"
                    self.status_line_idle_text = summary_text
                    action = None
                    if self._recent_export_paths:
                        action = ("Open exports folder", self.open_exports_folder)
                    self.show_banner(summary_text, banner_level, action=action)
                    self._trigger_auto_maintenance(self._last_scan_label or self.label_var.get().strip())
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
            skipped_perm = int(event.get("skipped_perm") or 0)
            skipped_long = int(event.get("skipped_toolong") or 0)
            skipped_ignored = int(event.get("skipped_ignored") or 0)
            self.skipped_detail_var.set(
                f"Skipped: perm={skipped_perm:,}, long={skipped_long:,}, ignored={skipped_ignored:,}"
            )
            if hasattr(self, "performance_var"):
                self.performance_var.set("Performance: —")
            self.progress_snapshot = None
            self._update_status_line(force=True)

    def _try_finalize_worker(self):
        if self.worker and not self.worker.is_alive():
            self.worker.join()
            self.worker = None
            self.stop_evt = None

    def refresh_all(self):
        self.refresh_drives(); self.refresh_jobs(); self.refresh_structure_view()

    def refresh_drives(self):
        self.tree.delete(*self.tree.get_children())
        con = sqlite3.connect(self.db_path.get()); cur = con.cursor()
        drive_labels: list[str] = []
        for row in cur.execute("""
            SELECT id,label,mount_path,COALESCE(drive_type,''),COALESCE(notes,''),COALESCE(serial,''),COALESCE(model,''),
                   ROUND(COALESCE(total_bytes,0)/1024.0/1024/1024,2)
            FROM drives ORDER BY id ASC
        """):
            self.tree.insert("", END, values=row)
            label_value = row[1] if len(row) > 1 else None
            if isinstance(label_value, str) and label_value:
                drive_labels.append(label_value)
        con.close()
        self._update_search_drive_options(drive_labels)
        self._update_reports_drive_options(drive_labels)

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
        api_proc = getattr(self, "api_process", None)
        if api_proc and api_proc.poll() is None:
            self.stop_local_api()
            try:
                api_proc.wait(timeout=2)
            except Exception:
                pass
        for toast in list(self._active_toasts):
            try:
                toast.destroy()
            except Exception:
                pass
        self._active_toasts.clear()
        unregister_log_listener(self._log_enqueue)
        if self._search_plus_thread and self._search_plus_thread.is_alive():
            if self._search_plus_cancel:
                self._search_plus_cancel.set()
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


class GPUProvisioningDialog(Toplevel):
    """Self-healing assistant for CUDA Execution Provider provisioning."""

    DOC_URL = (
        "https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html"
    )

    def __init__(self, app: "App", caps: Dict[str, object]):
        super().__init__(app.root)
        self.app = app
        self.caps: Dict[str, object] = dict(caps or {})
        self._retry_thread: Optional[threading.Thread] = None
        self.status_var = StringVar(
            value="CUDA Execution Provider failed to initialise."
        )
        self._check_vars: Dict[str, IntVar] = {}
        self._check_labels: Dict[str, ttk.Checkbutton] = {}

        self.title("GPU provisioning assistant")
        self.transient(app.root)
        self.resizable(False, False)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _event: self._on_close())

        container = ttk.Frame(self, padding=(20, 18))
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)

        ttk.Label(
            container,
            text=(
                "CUDA acceleration is currently unavailable. Review the checklist "
                "below, install missing components, then retry the CUDA provider."
            ),
            wraplength=420,
            style="Subtle.TLabel",
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        checklist = [
            ("driver", "NVIDIA driver present and recent"),
            ("cuda_toolkit", "CUDA Toolkit installed"),
            ("cudnn", "cuDNN installed"),
            ("msvc", "MSVC runtime present"),
        ]

        for idx, (key, text) in enumerate(checklist, start=1):
            var = IntVar(value=0)
            cb = ttk.Checkbutton(
                container,
                text=text,
                variable=var,
                state="disabled",
            )
            cb.grid(row=idx, column=0, sticky="w", pady=(6 if idx == 1 else 2, 0))
            self._check_vars[key] = var
            self._check_labels[key] = cb

        ttk.Label(
            container,
            text="Latest CUDA provider error:",
            style="StatusDanger.TLabel",
        ).grid(row=len(checklist) + 1, column=0, sticky="w", pady=(12, 4))

        self.error_box = ScrolledText(
            container,
            width=64,
            height=6,
            wrap="word",
            font=("Consolas", 9),
        )
        self.error_box.grid(row=len(checklist) + 2, column=0, sticky="nsew")
        container.rowconfigure(len(checklist) + 2, weight=1)
        self.error_box.configure(state="disabled", background="#f8f8f8")

        ttk.Label(
            container,
            textvariable=self.status_var,
            wraplength=420,
            justify="left",
        ).grid(row=len(checklist) + 3, column=0, sticky="w", pady=(12, 0))

        buttons = ttk.Frame(container)
        buttons.grid(row=len(checklist) + 4, column=0, sticky="ew", pady=(12, 0))
        for col in range(2):
            buttons.columnconfigure(col, weight=1)

        self.install_button = ttk.Button(
            buttons,
            text="Install CUDA Toolkit (winget)",
            command=self._install_cuda_toolkit,
        )
        self.install_button.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            buttons,
            text="Open CUDA/cuDNN requirements",
            command=self._open_docs,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        self.retry_button = ttk.Button(
            buttons,
            text="Retry CUDA provider",
            command=self._retry_cuda,
        )
        self.retry_button.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.directml_button = ttk.Button(
            buttons,
            text="Use DirectML instead",
            command=self._use_directml,
        )
        self.directml_button.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Button(buttons, text="Close", command=self._on_close).grid(
            row=2, column=1, sticky="e", pady=(12, 0)
        )

        self.update_caps(self.caps)

    # ------------------------------------------------------------------
    def update_caps(self, caps: Dict[str, object]) -> None:
        self.caps = dict(caps or {})
        hints = self.caps.get("cuda_dependency_hints") or {}
        label_suffix = {
            True: " — OK",
            False: " — missing",
            None: " — verify",
        }
        for key, cb in self._check_labels.items():
            status = hints.get(key)
            base_text = cb.cget("text").split(" — ")[0]
            suffix = label_suffix.get(status if status in label_suffix else None, "")
            cb.configure(text=base_text + suffix)
            var = self._check_vars[key]
            var.set(1 if status else 0)

        error_text = self.caps.get("onnx_cuda_error")
        self._set_error_text(
            str(error_text).strip() if error_text else "No CUDA error was reported."
        )

        if self.caps.get("onnx_cuda_ok"):
            self.status_var.set(
                "CUDA Execution Provider initialised successfully."
            )
            self.retry_button.configure(state="disabled")
        else:
            self.status_var.set(
                "CUDA provider is unavailable. Install missing dependencies or use DirectML as a fallback."
            )
            self.retry_button.configure(state="normal")

        if self.caps.get("onnx_directml_ok"):
            self.directml_button.configure(state="normal")
        else:
            self.directml_button.configure(state="disabled")

    def _set_error_text(self, text: str) -> None:
        self.error_box.configure(state="normal")
        self.error_box.delete("1.0", "end")
        self.error_box.insert("1.0", text or "(empty)")
        self.error_box.configure(state="disabled")

    def _install_cuda_toolkit(self) -> None:
        if not sys.platform.startswith("win"):
            messagebox.showinfo(
                "Unsupported platform",
                "winget installation is only available on Windows.",
            )
            return
        if not winget_available():
            messagebox.showerror(
                "winget unavailable",
                "winget is not available. Install winget or install the CUDA Toolkit manually.",
            )
            return
        command = ["winget", "install", "-e", "--id", "Nvidia.CUDA"]
        creation_flags = 0
        if hasattr(subprocess, "CREATE_NEW_CONSOLE"):
            creation_flags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        try:
            subprocess.Popen(command, creationflags=creation_flags)  # type: ignore[arg-type]
        except Exception as exc:
            messagebox.showerror("CUDA install", f"Failed to launch winget: {exc}")
            return
        log("Launched winget install for Nvidia.CUDA")
        self.status_var.set(
            "winget installation launched. Follow the CUDA installer window to complete setup."
        )

    def _open_docs(self) -> None:
        try:
            webbrowser.open(self.DOC_URL)
        except Exception:
            messagebox.showerror(
                "Open documentation", "Unable to launch the default browser."
            )

    def _retry_cuda(self) -> None:
        if self._retry_thread and self._retry_thread.is_alive():
            return

        self.status_var.set("Retrying CUDA provider…")
        self.retry_button.configure(state="disabled")

        def worker():
            error: Optional[str] = None
            snapshot: Optional[Dict[str, object]] = None
            try:
                snapshot = probe_gpu(refresh=True)
            except Exception as exc:  # pragma: no cover - defensive
                error = str(exc)
            self.after(0, lambda: self._retry_finished(snapshot, error))

        self._retry_thread = threading.Thread(target=worker, daemon=True)
        self._retry_thread.start()

    def _retry_finished(
        self,
        caps: Optional[Dict[str, object]],
        error: Optional[str],
    ) -> None:
        self._retry_thread = None
        if caps is None:
            if error:
                messagebox.showerror("CUDA retry failed", error)
                self.status_var.set(f"Retry failed: {error}")
            self.retry_button.configure(state="normal")
            return

        self.app._refresh_gpu_status(force=False, caps=caps)
        self.update_caps(caps)
        if caps.get("onnx_cuda_ok"):
            messagebox.showinfo(
                "CUDA ready",
                "CUDA Execution Provider initialised successfully.",
            )
            self.app._gpu_prompted = False
            self.after(200, self._on_close)
        else:
            self.status_var.set(
                "CUDA provider is still unavailable. Review the checklist above."
            )
            self.retry_button.configure(state="normal")

    def _use_directml(self) -> None:
        self.app.use_directml_now(refresh=True)

    def _on_close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self.app._gpu_dialog_closed()


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
