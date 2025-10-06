# DiskScannerGUI.py — V8.2 (Yann)
# Requiert: Python 3.10+, mediainfo (CLI), sqlite3 (intégré), smartctl/ffmpeg optionnels, blake3 (pip)
# Dossier base: C:\Users\Administrator\VideoCatalog

import os, sys, json, time, shutil, sqlite3, threading, subprocess, zipfile, queue
from functools import lru_cache
from pathlib import Path
from datetime import datetime
from typing import Optional
from tkinter import (
    Tk, Label, Entry, Button, StringVar, IntVar, END, N, S, E, W,
    filedialog, messagebox, ttk, Menu
)

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
def log(s: str):
    os.makedirs(LOGS_DIR, exist_ok=True)
    line = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ')}] {s}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

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

def init_shard(shard_path: str):
    con = sqlite3.connect(shard_path)
    cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS files(
      id INTEGER PRIMARY KEY,
      path TEXT NOT NULL,
      size_bytes INTEGER,
      is_av INTEGER,
      hash_blake3 TEXT,
      media_json TEXT,
      integrity_ok INTEGER,
      mtime_utc TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
    CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash_blake3);
    """)
    # integrity_ok semantics:
    #   1 → MediaInfo/scan succeeded (or non-A/V file)
    #   0 → MediaInfo reported an error for the file
    #   NULL → MediaInfo not executed (tool missing/disabled)
    con.commit()
    con.close()

# ---------------- Scanner worker ----------------
class ScannerWorker(threading.Thread):
    def __init__(self, label: str, mount: str, notes: str, dtype: str,
                 db_catalog: str, blake_for_av: bool, stop_evt: threading.Event,
                 event_queue: "queue.Queue[dict]"):
        super().__init__(daemon=False)
        self.label = label
        self.mount = mount
        self.notes = notes
        self.dtype = dtype
        self.db_catalog = db_catalog
        self.blake_for_av = blake_for_av
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
        all_files = []
        for root_dir, _, files in os.walk(mount):
            for f in files:
                all_files.append(os.path.join(root_dir, f))
        total_all = len(all_files)
        total_av = sum(1 for p in all_files if Path(p).suffix.lower() in AV_EXTS)

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
            batch = []
            BATCH_SIZE = 200

            with sqlite3.connect(str(shard_path)) as shard:
                scur = shard.cursor()
                scur.execute("DELETE FROM files")

                def flush():
                    if not batch:
                        return
                    scur.executemany("""INSERT INTO files(path,size_bytes,is_av,hash_blake3,media_json,integrity_ok,mtime_utc)
                                        VALUES(?,?,?,?,?,?,?)""", batch)
                    shard.commit()
                    batch.clear()

                for idx, fp in enumerate(all_files, start=1):
                    if self.stop_evt.is_set():
                        break
                    try:
                        p = Path(fp)
                        ext = p.suffix.lower()
                        st = p.stat()
                        size = st.st_size
                        mtime = datetime.utcfromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")

                        if ext in AV_EXTS:
                            mi = mediainfo_json(fp)
                            hb3 = blake3_hash(fp) if self.blake_for_av else None
                            if mi is None:
                                ok = None
                            else:
                                ok = 1 if "error" not in mi else 0
                            batch.append((fp, size, 1, hb3, json.dumps(mi, ensure_ascii=False) if mi else None, ok, mtime))
                            done_av += 1
                        else:
                            batch.append((fp, size, 0, None, None, 1, mtime))

                        if len(batch) >= BATCH_SIZE:
                            flush()

                        if idx % 500 == 0 or idx == total_all:
                            self._put({
                                "type": "progress",
                                "done_av": done_av,
                                "total_av": total_av,
                                "done_all": idx,
                                "total_all": total_all
                            })
                    except Exception as e:
                        log(f"[job {self.job_id}] error on {fp}: {e}")
                        batch.append((fp, None, 0, None, json.dumps({"error": str(e)}), 0, None))

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
        self.db_path = StringVar(value=DB_DEFAULT)

        # inputs
        self.path_var  = StringVar()
        self.label_var = StringVar()
        self.type_var  = StringVar()
        self.notes_var = StringVar()
        self.blake_var = IntVar(value=0)

        # worker state
        self.stop_evt: Optional[threading.Event] = None
        self.worker: Optional[ScannerWorker] = None
        self.worker_queue: "queue.Queue[dict]" = queue.Queue()
        self._closing = False

        self._build_menu()
        self._build_form()
        self._build_tables()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._poll_worker_queue)

        for d in (SCANS_DIR, LOGS_DIR, EXPORTS_DIR, SHARDS_DIR):
            os.makedirs(d, exist_ok=True)
        init_catalog(self.db_path.get())
        self.refresh_all()

    # ----- UI builders
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
        r=0
        Label(self.root, text="Current DB:").grid(row=r, column=0, sticky=E, padx=6, pady=4)
        Label(self.root, textvariable=self.db_path).grid(row=r, column=1, columnspan=3, sticky=W, padx=6)
        Button(self.root, text="Browse…", command=self.db_open).grid(row=r, column=4, sticky=W, padx=6)
        r+=1

        Label(self.root, text="Mount or Network path (e.g., E:\\ or \\\\server\\share):").grid(row=r, column=0, sticky=E, padx=6, pady=4)
        Entry(self.root, textvariable=self.path_var, width=70).grid(row=r, column=1, columnspan=3, sticky=W, padx=6)
        Button(self.root, text="Browse…", command=self.choose_path).grid(row=r, column=4, sticky=W, padx=6)
        r+=1

        Label(self.root, text="Disk Label (your own name):").grid(row=r, column=0, sticky=E, padx=6, pady=4)
        Entry(self.root, textvariable=self.label_var, width=30).grid(row=r, column=1, sticky=W, padx=6)

        self.presets = self._load_types()
        self.presets_combo = ttk.Combobox(self.root, values=self.presets, textvariable=self.type_var, width=30)
        self.presets_combo.grid(row=r, column=2, sticky=W, padx=6)
        Button(self.root, text="Presets ▾", command=self._reload_presets).grid(row=r, column=3, sticky=W, padx=6)
        Button(self.root, text="Save Type", command=self._save_current_type).grid(row=r, column=4, sticky=W, padx=6)
        r+=1

        Label(self.root, text="Drive Type (free text):").grid(row=r, column=0, sticky=E, padx=6, pady=4)
        Entry(self.root, textvariable=self.type_var, width=30).grid(row=r, column=1, sticky=W, padx=6)
        r+=1

        Label(self.root, text="Notes (short description):").grid(row=r, column=0, sticky=E, padx=6, pady=4)
        Entry(self.root, textvariable=self.notes_var, width=70).grid(row=r, column=1, columnspan=3, sticky=W, padx=6)
        r+=1

        ttk.Checkbutton(self.root, text="BLAKE3 for A/V (optional, slower)", variable=self.blake_var).grid(row=r, column=0, sticky=W, padx=10)
        Button(self.root, text="Scan (Auto)", command=self.start_scan).grid(row=r, column=1, sticky=W, padx=6)
        Button(self.root, text="Rescan (Delete shard + Scan)", command=self.rescan).grid(row=r, column=2, sticky=W, padx=6)
        Button(self.root, text="STOP", command=self.stop_scan).grid(row=r, column=3, sticky=W, padx=6)
        Button(self.root, text="Export Current Catalog DB", command=self.export_db).grid(row=r, column=4, sticky=W, padx=6)

    def _build_tables(self):
        Label(self.root, text="Drives in catalog:").grid(row=6, column=0, sticky=W, padx=6)
        cols = ("id","label","mount","type","notes","serial","model","totalGB")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", height=10)
        for c in cols: self.tree.heading(c, text=c)
        self.tree.grid(row=7, column=0, columnspan=5, sticky=(N,S,E,W), padx=6)
        self.root.grid_rowconfigure(7, weight=1); self.root.grid_columnconfigure(2, weight=1)

        Button(self.root, text="Delete Selected Drive (catalog only)", command=self.del_drive).grid(row=8, column=0, sticky=W, padx=6, pady=4)
        Button(self.root, text="Update Selected Drive Info", command=self.update_drive).grid(row=8, column=1, sticky=W, padx=6, pady=4)
        Button(self.root, text="Open Shard of Selected Drive", command=self.open_shard_selected).grid(row=8, column=4, sticky=E, padx=6, pady=4)

        Label(self.root, text="Jobs:").grid(row=9, column=0, sticky=W, padx=6)
        jcols=("id","drive_label","status","done_av","total_av","total_all","started_at","finished_at","duration","message")
        self.jobs = ttk.Treeview(self.root, columns=jcols, show="headings", height=6)
        for c in jcols: self.jobs.heading(c, text=c)
        self.jobs.grid(row=10, column=0, columnspan=5, sticky=(N,S,E,W), padx=6)
        Button(self.root, text="Delete Selected Job", command=self.del_job).grid(row=11, column=0, sticky=W, padx=6, pady=4)

        self.status_var = StringVar(value="Ready.")
        self.progress = ttk.Progressbar(self.root, orient="horizontal", mode="determinate", length=600)
        self.progress.grid(row=12, column=0, columnspan=3, padx=6, pady=6, sticky=W)
        Label(self.root, textvariable=self.status_var).grid(row=12, column=4, sticky=E, padx=6)

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
        self.refresh_all(); self.status_var.set(f"Updated drive {new_label}.")

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
        self.worker = ScannerWorker(
            label=self.label_var.get().strip(),
            mount=self.path_var.get().strip(),
            notes=self.notes_var.get().strip(),
            dtype=self.type_var.get().strip(),
            db_catalog=self.db_path.get(),
            blake_for_av=bool(self.blake_var.get()),
            stop_evt=self.stop_evt,
            event_queue=self.worker_queue
        )
        self.status_var.set("Scanning…")
        self.progress["value"] = 0
        self.progress["maximum"] = 1
        self.worker.start()
        self.root.after(800, self.refresh_jobs)

    def stop_scan(self):
        if self.stop_evt:
            self.stop_evt.set()
        self.status_var.set("Stopping…")

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
            self.progress["maximum"] = max(1, event.get("total_all", 1))
            self.progress["value"] = event.get("done_all", 0)
            total_av = event.get("total_av", 0)
            done_av = event.get("done_av", 0)
            done_all = event.get("done_all", 0)
            total_all = event.get("total_all", 0)
            self.status_var.set(f"Scanned AV {done_av}/{total_av} | ALL {done_all}/{total_all}")
        elif etype == "status":
            self.status_var.set(event.get("message", ""))
        elif etype == "refresh_jobs":
            self.refresh_jobs()
        elif etype == "error":
            messagebox.showerror(event.get("title", "Error"), event.get("message", ""))
        elif etype == "done":
            self._await_worker_completion()
            status = event.get("status", "Ready")
            if status == "Done":
                self.status_var.set("Ready.")
            elif status == "Stopped":
                self.status_var.set("Scan stopped.")
            else:
                self.status_var.set(status)
            self.progress["value"] = 0
            self.progress["maximum"] = 1

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
                self.status_var.set("Ready.")

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        if self.worker and self.worker.is_alive():
            if self.stop_evt:
                self.stop_evt.set()
            self.status_var.set("Stopping…")
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
    try:
        style = ttk.Style()
        if "vista" in style.theme_names(): style.theme_use("vista")
    except Exception: pass
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
