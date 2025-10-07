# DiskScannerGUI.py — V8.2 (Yann)
# Requiert: Python 3.10+, mediainfo (CLI), sqlite3 (intégré), smartctl/ffmpeg optionnels, blake3 (pip)
# Dossier base: C:\Users\Administrator\VideoCatalog

import os, sys, json, time, shutil, sqlite3, threading, subprocess, zipfile, queue
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from datetime import datetime
from typing import Optional
from tkinter import (
    Tk, StringVar, IntVar, END, N, S, E, W,
    filedialog, messagebox, ttk, Menu, Spinbox
)
from tkinter.scrolledtext import ScrolledText

# ---------------- Config & constantes ----------------
BASE_DIR   = r"C:\Users\Administrator\VideoCatalog"
DB_DEFAULT = str(Path(BASE_DIR) / "catalog.db")
SCANS_DIR  = str(Path(BASE_DIR) / "scans")
LOGS_DIR   = str(Path(BASE_DIR) / "logs")
EXPORTS_DIR= str(Path(BASE_DIR) / "exports")
SHARDS_DIR = str(Path(BASE_DIR) / "shards")
TYPES_JSON = str(Path(BASE_DIR) / "drive_types.json")
LOG_FILE   = str(Path(LOGS_DIR) / "scanner.log")

VIDEO_EXTS = {
    ".mp4",".mkv",".avi",".mov",".wmv",".m4v",".ts",".m2ts",".webm",".mpg",".mpeg",".mp2",".vob",".flv",".3gp",".ogv",".mts",".m2t"
}
AUDIO_EXTS = {
    ".mp3",".flac",".aac",".m4a",".wav",".wma",".ogg",".opus",".alac",".aiff",".ape",".dsf",".dff"
}
AV_EXTS = VIDEO_EXTS | AUDIO_EXTS

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

@lru_cache(maxsize=None)
def has_cmd(cmd: str) -> bool:
    try:
        subprocess.check_output([cmd, "--version"], stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False

def disk_usage_bytes(path: Path):
    total, used, free = shutil.disk_usage(path)
    return int(total), int(free)

def mediainfo_json(file_path: str) -> Optional[dict]:
    if not has_cmd("mediainfo"):
        return None
    try:
        out = subprocess.check_output(
            ["mediainfo","--Output=JSON", file_path],
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

def try_smart_overview() -> Optional[str]:
    if not has_cmd("smartctl"): return None
    acc = {"scan": None, "details": []}
    try:
        out = subprocess.check_output(["smartctl","--scan-open"], text=True, stderr=subprocess.STDOUT)
        acc["scan"] = out
        for n in range(0,20):
            try:
                out = subprocess.check_output(
                    ["smartctl","-i","-H","-A","-j", fr"\\.\PhysicalDrive{n}"],
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
    def __init__(self, label: str, mount: str, notes: str, dtype: str,
                 db_catalog: str, blake_for_av: bool, max_workers: int,
                 stop_evt: threading.Event, event_queue: "queue.Queue[dict]"):
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

    def _put(self, payload: dict):
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            pass

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

        shard_path = Path(SHARDS_DIR) / f"{self.label}.db"
        os.makedirs(SHARDS_DIR, exist_ok=True)

        self._put({"type": "status", "message": "Enumerating files…"})

        def iter_files(base: Path):
            for root_dir, _, files in os.walk(base):
                for name in files:
                    yield os.path.join(root_dir, name)

        all_files: list[str] = []
        total_av = 0
        for fp in iter_files(mount):
            all_files.append(fp)
            if Path(fp).suffix.lower() in AV_EXTS:
                total_av += 1
        total_all = len(all_files)

        started_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        smart_blob = try_smart_overview()
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
                    if processed_all % 200 == 0 or processed_all == total_all:
                        self._put({
                            "type": "progress",
                            "done_av": done_av,
                            "total_av": total_av,
                            "done_all": processed_all,
                            "total_all": total_all
                        })

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
                        if ext in AV_EXTS:
                            mi = mediainfo_json(fp)
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

            duration = int(time.perf_counter() - t0)
            status = "Stopped" if self.stop_evt.is_set() else "Done"
            ccur.execute("""UPDATE jobs SET finished_at=datetime('now'), status=?, done_av=?, duration_sec=?, message=?
                            WHERE id=?""",
                         (status, done_av, duration, f"Completed ({done_av}/{total_av}); scanned {total_all} files total", self.job_id))
            catalog.commit()

            self._put({"type": "refresh_jobs"})
            final_msg = "Scan stopped." if self.stop_evt.is_set() else "Scan complete."
            self._put({"type": "status", "message": final_msg})
            self._put({"type": "done", "status": status})

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

        register_log_listener(self._log_enqueue)
        self._load_existing_logs()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._poll_worker_queue)

        for d in (SCANS_DIR, LOGS_DIR, EXPORTS_DIR, SHARDS_DIR):
            os.makedirs(d, exist_ok=True)
        init_catalog(self.db_path.get())
        self.refresh_all()

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
        menubar.add_cascade(label="Tools", menu=mtools)

    def _build_form(self):
        self.content = ttk.Frame(self.root, padding=(20, 18), style="Content.TFrame")
        self.content.grid(row=0, column=0, sticky=(N, S, E, W))
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
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
    def db_new(self):
        fn = filedialog.asksaveasfilename(defaultextension=".db", initialdir=BASE_DIR, title="Create new catalog DB")
        if not fn: return
        self.db_path.set(fn); init_catalog(fn); self.refresh_all()

    def db_open(self):
        fn = filedialog.askopenfilename(initialdir=BASE_DIR, title="Open catalog DB", filetypes=[("SQLite DB","*.db"),("All","*.*")])
        if not fn: return
        self.db_path.set(fn); init_catalog(fn); self.refresh_all()

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
        self._launch_worker()

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
            old = Path(SHARDS_DIR) / f"{old_label}.db"
            new = Path(SHARDS_DIR) / f"{new_label}.db"
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
        sp = str(Path(SHARDS_DIR) / f"{label}.db")
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
        self._launch_worker()

    def rescan(self):
        label = self.label_var.get().strip()
        mount = self.path_var.get().strip()
        if not (label and mount):
            messagebox.showerror("Missing", "Please fill Mount Path and Disk Label."); return
        shard_path = Path(SHARDS_DIR) / f"{label}.db"
        for _ in range(5):
            try:
                if shard_path.exists(): os.remove(shard_path)
                break
            except PermissionError: time.sleep(0.3)
        self._launch_worker()

    def _launch_worker(self):
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
            event_queue=self.worker_queue
        )
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
            total_all = event.get("total_all", 0) or 0
            done_all = event.get("done_all", 0) or 0
            total_av = event.get("total_av", 0) or 0
            done_av = event.get("done_av", 0) or 0

            self.total_progress["maximum"] = max(1, total_all)
            self.total_progress["value"] = done_all
            self.av_progress["maximum"] = max(1, total_av or 1)
            self.av_progress["value"] = done_av

            overall_pct = (done_all / total_all * 100) if total_all else 0.0
            av_pct = (done_av / total_av * 100) if total_av else 0.0

            self.progress_percent_var.set(f"{overall_pct:.0f}%")
            self.total_percent_var.set(f"{overall_pct:.0f}%")
            self.av_percent_var.set(f"{av_pct:.0f}%" if total_av else "—")

            if total_all:
                detail = f"{done_all:,}/{total_all:,} files · {done_av:,}/{total_av:,} AV"
                status_text = f"Scanning • {done_all:,}/{total_all:,} files"
            else:
                detail = "Gathering files…"
                status_text = "Scanning…"
            self.progress_detail_var.set(detail)
            self._set_status(status_text, "accent")
            self._update_progress_styles(overall_pct, av_pct if total_av else 0.0)
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
        elif etype == "refresh_jobs":
            self.refresh_jobs()
        elif etype == "error":
            messagebox.showerror(event.get("title", "Error"), event.get("message", ""))
        elif etype == "log":
            self._append_log(event.get("line", ""))
        elif etype == "done":
            self._await_worker_completion()
            status = event.get("status", "Ready")
            if status == "Done":
                self._set_status("Ready.", "success")
            elif status == "Stopped":
                self._set_status("Scan stopped.", "warning")
            else:
                self._set_status(status)
            self.progress_detail_var.set("No scan running.")
            self.progress_percent_var.set("0%")
            self.total_percent_var.set("0%")
            self.av_percent_var.set("—")
            self.total_progress["value"] = 0
            self.total_progress["maximum"] = 1
            self.av_progress["value"] = 0
            self.av_progress["maximum"] = 1
            self._update_progress_styles(0, 0)

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

# ---------------- main ----------------
def main():
    os.makedirs(BASE_DIR, exist_ok=True)
    for d in (SCANS_DIR, LOGS_DIR, EXPORTS_DIR, SHARDS_DIR): os.makedirs(d, exist_ok=True)
    init_catalog(DB_DEFAULT)
    root = Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
