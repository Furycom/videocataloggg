# DiskScannerGUI.py — build 2025-10-05_dbmanager
# - Catalogue TOUS les fichiers (non-AV : path/size/mtime), AV : MediaInfo (timeout 30 s).
# - Optionnel: BLAKE3 pour AV (désactivé par défaut).
# - Jobs: total_av et total_all (progress AV).
# - Gestion DB : Switch/New/Reset/Delete/Open folder + Reset EVERYTHING (catalog + shards).
# - Persistance du chemin DB via settings.json.

import os, sys, json, time, shutil, sqlite3, threading, subprocess, queue, re, traceback, zipfile
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from tkinter import Tk, Label, Entry, Button, StringVar, filedialog, messagebox, Menu, Checkbutton, IntVar
from tkinter import ttk, END, N, S, E, W
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

# ========= Constantes / chemins =========
BASE_DIR   = r"C:\Users\Administrator\VideoCatalog".replace("\\\\","\\")
DEFAULT_DB = str(Path(BASE_DIR) / "catalog.db")
SETTINGS   = str(Path(BASE_DIR) / "settings.json")
SHARDS_DIR = str(Path(BASE_DIR) / "shards")
EXPORTS    = str(Path(BASE_DIR) / "exports")
LOGS_DIR   = str(Path(BASE_DIR) / "logs")
CATS_JSON  = str(Path(BASE_DIR) / "drive_types.json")

MEDIAINFO_TIMEOUT_S = 30
MAX_WORKERS_AV      = 4
WRITE_BATCH         = 800
JOB_HISTORY_PER_DRIVE = 50

VIDEO = {
    '.mp4','.m4v','.mkv','.mov','.avi','.wmv',
    '.ts','.m2ts','.mts','.mxf','.webm','.flv',
    '.mpg','.mpeg','.mpe','.vob','.3gp','.3g2',
    '.mjp2','.f4v','.ogv','.divx','.xvid'
}
AUDIO = {
    '.mp3','.flac','.aac','.m4a','.wav','.w64','.bwf',
    '.ogg','.oga','.opus','.wma','.aiff','.aif','.aifc',
    '.alac','.ape','.ac3','.eac3','.dts','.dtsma',
    '.mka','.mpc','.wv','.spx','.midi','.mid','.caf'
}

# ========= Helpers =========
def ensure_dirs():
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(SHARDS_DIR, exist_ok=True)
    os.makedirs(EXPORTS, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

def load_settings()->dict:
    ensure_dirs()
    if os.path.exists(SETTINGS):
        try:
            with open(SETTINGS,'r',encoding='utf-8') as f: return json.load(f)
        except: pass
    return {"catalog_db": DEFAULT_DB}

def save_settings(cfg:dict):
    ensure_dirs()
    with open(SETTINGS,'w',encoding='utf-8') as f: json.dump(cfg, f, ensure_ascii=False, indent=2)

def current_db_path()->str:
    cfg = load_settings()
    p = cfg.get("catalog_db") or DEFAULT_DB
    return p

def set_current_db(path:str):
    cfg = load_settings()
    cfg["catalog_db"] = path
    save_settings(cfg)

def log_line(msg: str):
    try:
        with open(Path(LOGS_DIR) / "scanner.log", "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

def run(cmd:List[str])->Tuple[int,str,str]:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
    out, err = p.communicate()
    return p.returncode, out, err

def has_cmd(cmd:str)->bool:
    try:
        subprocess.check_output([cmd, "--version"], stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False

def compact_db(path: str):
    try:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
        conn.execute("VACUUM;")
        conn.commit()
        conn.close()
    except Exception as e:
        log_line(f"compact_db({path}) error: {e}")

def ensure_categories():
    if not os.path.exists(CATS_JSON):
        with open(CATS_JSON, "w", encoding="utf-8") as f:
            json.dump(["HDD 3.5","HDD 2.5","SSD SATA","SSD NVMe","USB enclosure"], f, ensure_ascii=False, indent=2)

def load_categories()->List[str]:
    ensure_categories()
    try:
        with open(CATS_JSON,"r",encoding="utf-8") as f:
            lst = json.load(f)
            if isinstance(lst,list): return sorted({str(x).strip() for x in lst if str(x).strip()})
    except: pass
    return []

def save_category_if_new(cat:str):
    cat = (cat or "").strip()
    if not cat: return
    cats = load_categories()
    if cat not in cats:
        cats.append(cat)
        with open(CATS_JSON,"w",encoding="utf-8") as f:
            json.dump(sorted(cats), f, ensure_ascii=False, indent=2)

def normalize_mount_path(p:str)->str:
    p = (p or "").strip().strip('"').strip("'")
    m = re.match(r"^[A-Za-z]:$", p)
    if m: p = p + "\\"
    return p

def pretty_duration(sec: float) -> str:
    if sec >= 60: return str(timedelta(seconds=int(sec)))
    return f"{sec:0.3f}s"

# ========= DB: schéma & init =========
def init_catalog_db(path: str):
    ensure_dirs()
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS drives(
        id INTEGER PRIMARY KEY,
        label TEXT NOT NULL,
        mount_path TEXT NOT NULL,
        fs_format TEXT,
        total_bytes INTEGER,
        free_bytes INTEGER,
        smart_scan TEXT,
        scanned_at TEXT NOT NULL,
        drive_type TEXT,
        notes TEXT,
        scan_mode TEXT,
        serial TEXT,
        model  TEXT
    );
    CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY,
        drive_label TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,     -- Running|Done|Canceled|Error
        total_files INTEGER,      -- legacy (progress AV)
        done_files  INTEGER,      -- legacy (progress AV)
        message TEXT,
        duration_sec INTEGER,
        total_av INTEGER,
        total_all INTEGER
    );
    """)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_drives_label_unique ON drives(label);")
    # auto-migr
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
    for col in ("total_av","total_all","duration_sec"):
        if col not in cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} INTEGER;")
    conn.commit()
    return conn

def drop_all_tables(path:str):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    # supprime / recrée le schéma propre
    cur.executescript("""
    DROP TABLE IF EXISTS jobs;
    DROP TABLE IF EXISTS drives;
    """)
    conn.commit()
    conn.close()
    # recrée
    init_catalog_db(path).close()

def init_shard_schema(conn: sqlite3.Connection):
    c = conn.cursor()
    c.executescript("""
    PRAGMA journal_mode=WAL;
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
    CREATE INDEX IF NOT EXISTS idx_files_hash  ON files(hash_blake3);
    """)
    conn.commit()

# ========= Media / SMART =========
def mediainfo_json_timeout(file_path: str, timeout_s: int = MEDIAINFO_TIMEOUT_S) -> Tuple[Optional[dict], Optional[str]]:
    if not has_cmd("mediainfo"):
        return None, "mediainfo_not_available"
    try:
        p = subprocess.Popen(["mediainfo", "--Output=JSON", file_path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, shell=False)
        out, err = p.communicate(timeout=timeout_s)
        if p.returncode == 0 and out.strip():
            try:
                return json.loads(out), None
            except Exception as e:
                return None, f"json_parse_error:{e}"
        return None, f"mediainfo_exitcode:{p.returncode}"
    except subprocess.TimeoutExpired:
        try: p.kill()
        except Exception: pass
        return None, "mediainfo_timeout"

def smart_json()->Optional[Dict[str,Any]]:
    if not has_cmd("smartctl"): return None
    acc = {"scan":None,"details":[]}
    code,out,err = run(["smartctl","--scan-open"])
    if code==0: acc["scan"]=out
    for n in range(0,20):
        code,out,err = run(["smartctl","-i","-H","-A","-j", fr"\\.\PhysicalDrive{n}"])
        if code==0 and out.strip():
            try: acc["details"].append(json.loads(out))
            except: pass
    return acc

def match_serial_model(sacc:Optional[Dict[str,Any]], total:int)->Tuple[Optional[str],Optional[str]]:
    if not sacc or not sacc.get("details"): return None,None
    for d in sacc["details"]:
        try:
            cap = int(d.get("user_capacity",{}).get("bytes",0))
            if cap and abs(cap-total) <= max(1, int(0.01*total)):
                return d.get("serial_number"), d.get("model_name") or d.get("model_family")
        except: pass
    return None,None

def is_video(p:str)->bool: p=p.lower(); return any(p.endswith(e) for e in VIDEO)
def is_audio(p:str)->bool: p=p.lower(); return any(p.endswith(e) for e in AUDIO)
def is_av(p:str)->bool: p=p.lower(); return any(p.endswith(e) for e in VIDEO | AUDIO)

def shard_path(label:str)->str:
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in label).strip().replace(" ","_")
    return str(Path(SHARDS_DIR)/f"{safe}.db")

# ========= Scanner =========
class Scanner:
    def __init__(self, cat_db:str, status_cb=None, progress_cb=None, pause_ev:threading.Event=None, stop_ev:threading.Event=None, do_hash_av:bool=False):
        self.cat_db=cat_db
        self.status_cb=status_cb; self.progress_cb=progress_cb
        self.pause_ev=pause_ev or threading.Event(); self.stop_ev=stop_ev or threading.Event()
        self.do_hash_av = do_hash_av

    def _status(self,msg): 
        if self.status_cb: self.status_cb(msg)
        log_line(msg)
    def _progress(self,d,t):
        if self.progress_cb: self.progress_cb(d,t)

    def _walk(self, mount:Path)->List[str]:
        files=[]
        for root,dirs,fs in os.walk(str(mount)):
            if self.stop_ev.is_set(): break
            low = root.lower()
            if "$recycle.bin" in low or "system volume information" in low:
                continue
            for f in fs:
                files.append(os.path.join(root,f))
        return files

    def _hash_b3(self, fp:str)->Optional[str]:
        if not self.do_hash_av: return None
        try:
            from blake3 import blake3
        except Exception:
            return None
        h = blake3()
        try:
            with open(fp,"rb") as f:
                for chunk in iter(lambda: f.read(1024*1024), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def _row_non_av(self, label:str, fp:str):
        try:
            size=os.path.getsize(fp)
            mtime=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(fp)))
            mj = json.dumps({"skipped":"non_av"}, ensure_ascii=False)
            return (label, fp, size, None, mj, 1, mtime)
        except Exception as e:
            return (label, fp, None, None, json.dumps({"error":str(e)}, ensure_ascii=False), 0, None)

    def _row_av(self, label:str, fp:str):
        try:
            size=os.path.getsize(fp)
            mtime=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(fp)))
            mi, err = mediainfo_json_timeout(fp, MEDIAINFO_TIMEOUT_S)
            hb3 = self._hash_b3(fp)
            if mi is not None:
                return (label, fp, size, hb3, json.dumps(mi,ensure_ascii=False), 1, mtime)
            else:
                return (label, fp, size, hb3, json.dumps({"mediainfo_error":err},ensure_ascii=False), 0, mtime)
        except Exception as e:
            return (label, fp, None, None, json.dumps({"error":str(e)},ensure_ascii=False), 0, None)

    def scan_auto(self, label:str, mount_path:str, drive_type:str, notes:str):
        ensure_dirs()
        cat = init_catalog_db(self.cat_db)

        norm_path = normalize_mount_path(mount_path)
        mnt = Path(norm_path)
        if not mnt.exists():
            raise RuntimeError(f"Mount path not found: {norm_path}")

        total,used,free = shutil.disk_usage(mnt)
        sj  = smart_json()
        sjs = json.dumps(sj, ensure_ascii=False) if sj else None
        serial, model = (None, None)
        try:
            serial, model = match_serial_model(sj, total)
        except Exception:
            pass
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Upsert drive
        row = cat.execute("SELECT id FROM drives WHERE label=?", (label,)).fetchone()
        if row is None:
            cat.execute("""INSERT INTO drives(label,mount_path,fs_format,total_bytes,free_bytes,smart_scan,scanned_at,
                                             drive_type,notes,scan_mode,serial,model)
                           VALUES(?,?,?,?,?,?,?, ?,?,?,?,?)""",
                        (label,str(mnt),None,int(total),int(free),sjs,now,drive_type,notes,"Auto-AV",serial,model))
        else:
            cat.execute("""UPDATE drives SET mount_path=?, total_bytes=?, free_bytes=?, smart_scan=?,
                           scanned_at=?, drive_type=?, notes=?, scan_mode='Auto-AV',
                           serial=COALESCE(?,serial), model=COALESCE(?,model) WHERE label=?""",
                        (str(mnt),int(total),int(free),sjs,now,drive_type,notes,serial,model,label))
        cat.commit()

        # Create job
        cat.execute("""INSERT INTO jobs(drive_label, started_at, status, total_files, done_files, message, duration_sec, total_av, total_all)
                       VALUES(?, ?, 'Running', 0, 0, ?, 0, 0, 0)""",
                    (label, now, f"Scanning at {str(mnt)}"))
        job_id = cat.execute("SELECT last_insert_rowid()").fetchone()[0]
        cat.commit()

        # Enumerate all files
        self._status(f"[Job {job_id}] Pre-scanning at {str(mnt)} ...")
        all_files = self._walk(mnt)
        av_files  = [fp for fp in all_files if is_av(fp)]
        total_all = len(all_files)
        total_av  = len(av_files)

        cat.execute("UPDATE jobs SET total_av=?, total_all=?, message=? WHERE id=?",
                    (total_av, total_all, f"Found AV {total_av} / Total {total_all}", job_id))
        cat.commit()
        self._status(f"[Job {job_id}] Found AV {total_av} of TOTAL {total_all} at {str(mnt)}")
        self._progress(0,total_av)

        shard_file = shard_path(label)

        # Writer thread
        q: "queue.Queue[tuple]" = queue.Queue(maxsize=WRITE_BATCH*2)
        writer_done = threading.Event()
        def writer():
            conn = sqlite3.connect(shard_file, check_same_thread=False)
            init_shard_schema(conn)
            cur = conn.cursor()
            batch=[]
            try:
                while True:
                    try:
                        item = q.get(timeout=0.2)
                    except queue.Empty:
                        if writer_done.is_set(): break
                        continue
                    if item is None: break
                    batch.append(item)
                    if len(batch) >= WRITE_BATCH:
                        cur.executemany("""INSERT INTO files(drive_label,path,size_bytes,hash_blake3,media_json,integrity_ok,mtime_utc)
                                           VALUES(?,?,?,?,?,?,?)""", batch)
                        conn.commit(); batch.clear()
                if batch:
                    cur.executemany("""INSERT INTO files(drive_label,path,size_bytes,hash_blake3,media_json,integrity_ok,mtime_utc)
                                       VALUES(?,?,?,?,?,?,?)""", batch)
                    conn.commit()
            finally:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    conn.commit()
                    conn.execute("VACUUM;")
                    conn.commit()
                except Exception as e:
                    log_line(f"Shard compact error: {e}")
                conn.close()

        wt = threading.Thread(target=writer, daemon=True); wt.start()

        # 1) Enqueue TOUS les non-AV rapidement
        for fp in (fp for fp in all_files if not is_av(fp)):
            q.put(self._row_non_av(label, fp))

        # 2) Traiter les AV en pool
        done=0
        t0=time.perf_counter()
        canceled=False; failed=None
        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS_AV) as pool:
                futs=[pool.submit(self._row_av,label,fp) for fp in av_files]
                for fut in as_completed(futs):
                    while self.pause_ev.is_set() and not self.stop_ev.is_set():
                        time.sleep(0.1)
                    if self.stop_ev.is_set(): raise KeyboardInterrupt()
                    res=fut.result()
                    q.put(res)
                    done+=1
                    if done%50==0 or done==total_av:
                        elapsed=max(0.001, time.perf_counter()-t0)
                        rate=done/elapsed
                        self._status(f"[Job {job_id}] AV {done}/{total_av} | {rate:.1f} AV/s | Total {total_all}")
                        self._progress(done,total_av)
                        cat.execute("UPDATE jobs SET done_files=?, total_files=?, message=? WHERE id=?", 
                                    (done, total_av, f"AV {done}/{total_av} | Total {total_all}", job_id))
                        cat.commit()
        except KeyboardInterrupt:
            canceled=True
            self._status(f"[Job {job_id}] Cancel requested. Finalizing...")
        except Exception as e:
            failed=str(e)
            self._status(f"[Job {job_id}] Error: {e}")
        finally:
            writer_done.set(); q.put(None); wt.join(timeout=60)

            # Vérif de complétude: compter lignes shard
            try:
                connv = sqlite3.connect(shard_file)
                cnt = connv.execute("SELECT COUNT(*) FROM files WHERE drive_label=?", (label,)).fetchone()[0]
                connv.close()
            except Exception as e:
                cnt = -1
                log_line(f"[Job {job_id}] verify count error: {e}")

            elapsed = time.perf_counter() - t0
            elapsed_sec = max(0.001, elapsed)
            status = 'Done'
            msg = f"Completed AV {done}/{total_av} | Total {total_all}; rate ~ {(done/elapsed_sec):.1f} AV/s"
            if canceled: status='Canceled'; msg='User canceled'
            if failed:   status='Error';    msg=failed
            if cnt >= 0 and cnt != total_all:
                msg += f"  [WARN: shard rows={cnt} != total_all={total_all}]"
                log_line(f"[Job {job_id}] WARNING: shard rowcount {cnt} != total_all {total_all}")

            cat.execute("""UPDATE jobs 
                           SET status=?, finished_at=?, done_files=?, duration_sec=?, message=?, total_av=?, total_all=?, total_files=?
                           WHERE id=?""",
                        (status, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         done, int(elapsed_sec), msg, total_av, total_all, total_av, job_id))
            cat.commit()
            try:
                compact_db(self.cat_db)
                # Limiter l'historique
                rows = cat.execute("SELECT id FROM jobs WHERE drive_label=? ORDER BY id DESC",(label,)).fetchall()
                if len(rows) > JOB_HISTORY_PER_DRIVE:
                    to_del = tuple(r[0] for r in rows[JOB_HISTORY_PER_DRIVE:])
                    qmarks = ",".join("?" for _ in to_del)
                    cat.execute(f"DELETE FROM jobs WHERE id IN ({qmarks})", to_del); cat.commit()
            except Exception as e:
                log_line(f"Catalog compact/history error: {e}")
            cat.close()
            self._progress(total_av,total_av)
            self._status(f"[Job {job_id}] {status}. Duration { (str(timedelta(seconds=int(elapsed_sec))) if elapsed_sec>=60 else f'{elapsed_sec:0.3f}s') }.")

# ========= GUI =========
class App:
    def __init__(self, root: Tk):
        self.root=root
        root.title("Disk Scanner - Catalog GUI")
        root.geometry("1200x800"); root.minsize(1160,760)
        ensure_dirs(); ensure_categories()

        self.cat_db = current_db_path()
        init_catalog_db(self.cat_db)

        self.path_var=StringVar(); self.label_var=StringVar(); self.type_var=StringVar(); self.notes_var=StringVar()
        self.hash_av = IntVar(value=0)
        self.pause_ev=threading.Event(); self.stop_ev=threading.Event()

        # Menus
        menubar=Menu(root)
        dbm=Menu(menubar, tearoff=0)
        dbm.add_command(label="Show Current DB Path", command=self.show_db_path)
        dbm.add_command(label="Switch to Existing DB…", command=self.switch_db)
        dbm.add_command(label="Create New DB…", command=self.create_db)
        dbm.add_separator()
        dbm.add_command(label="Reset DB (drop all data)…", command=self.reset_db)
        dbm.add_command(label="Delete DB file…", command=self.delete_db_file)
        dbm.add_separator()
        dbm.add_command(label="Open DB Folder", command=self.open_db_folder)
        dbm.add_command(label="Reset EVERYTHING (DB + shards)…", command=self.reset_everything)
        menubar.add_cascade(label="Database", menu=dbm)

        tools=Menu(menubar, tearoff=0)
        tools.add_command(label="Open shards folder", command=lambda: os.startfile(SHARDS_DIR))
        tools.add_command(label="Compact Current DB", command=self.compact_catalog)
        menubar.add_cascade(label="Tools", menu=tools)
        root.config(menu=menubar)

        r=0
        Label(root,text=f"Current DB: {self.cat_db}").grid(row=r,column=0,columnspan=3,sticky=W,padx=10,pady=(8,2)); r+=1

        Label(root,text="Mount or Network path (e.g., E:\\ or \\\\server\\share):").grid(row=r,column=0,sticky=W,padx=10,pady=6)
        Entry(root,textvariable=self.path_var,width=70).grid(row=r,column=1,sticky=W,padx=10); Button(root,text="Browse...",command=self.choose_path).grid(row=r,column=2,padx=5); r+=1
        Label(root,text="Disk Label (your own name):").grid(row=r,column=0,sticky=W,padx=10,pady=6); Entry(root,textvariable=self.label_var,width=40).grid(row=r,column=1,sticky=W,padx=10); r+=1
        Label(root,text="Drive Type (free text):").grid(row=r,column=0,sticky=W,padx=10,pady=6); Entry(root,textvariable=self.type_var,width=40).grid(row=r,column=1,sticky=W,padx=10); r+=1
        Label(root,text="Notes (short description):").grid(row=r,column=0,sticky=W,padx=10,pady=6); Entry(root,textvariable=self.notes_var,width=70).grid(row=r,column=1,columnspan=2,sticky=W,padx=10); r+=1

        Checkbutton(root,text="BLAKE3 for A/V (optional, slower)",variable=self.hash_av).grid(row=r,column=0,sticky=W,padx=10)
        Button(root,text="Scan (Auto)",command=self.start_scan).grid(row=r,column=1,padx=10,pady=8,sticky=W)
        Button(root,text="Rescan (Delete shard + Scan)",command=self.start_rescan).grid(row=r,column=2,padx=10,pady=8,sticky=W); r+=1

        Button(root,text="Pause",command=self.pause).grid(row=r,column=1,padx=10,pady=8,sticky=W)
        Button(root,text="Resume",command=self.resume).grid(row=r,column=1,padx=80,pady=8,sticky=W)
        Button(root,text="STOP",command=self.stop).grid(row=r,column=2,padx=10,pady=8,sticky=W)
        Button(root,text="Export Current Catalog DB",command=self.export_catalog).grid(row=r,column=2,padx=140,pady=8,sticky=W); r+=1

        Label(root,text="Drives in catalog:").grid(row=r,column=0,sticky=W,padx=10); r+=1
        cols=("id","label","mount","type","notes","serial","model","totalGB","freeGB","scanned_at")
        self.tree=ttk.Treeview(root,columns=cols,show="headings",height=10)
        for c in cols: self.tree.heading(c,text=c)
        for name,w in zip(cols,(40,160,220,150,260,140,160,80,80,150)): self.tree.column(name,width=w,anchor="w")
        self.tree.grid(row=r,column=0,columnspan=3,sticky=(N,S,E,W),padx=10)
        self.tree.bind("<<TreeviewSelect>>",self.on_sel)
        self.tree.bind("<Double-1>",self.on_dbl)
        root.grid_rowconfigure(r,weight=1); root.grid_columnconfigure(1,weight=1); r+=1

        Button(root,text="Delete Selected Drive (catalog only)",command=self.del_drive).grid(row=r,column=0,padx=10,pady=6,sticky=W)
        Button(root,text="Update Selected Drive Info",command=self.update_drive).grid(row=r,column=1,padx=10,pady=6,sticky=W)
        Button(root,text="Open Shard of Selected Drive",command=self.open_shard_selected).grid(row=r,column=2,padx=10,pady=6,sticky=E); r+=1

        Label(root,text="Jobs:").grid(row=r,column=0,sticky=W,padx=10); r+=1
        jcols=("id","drive_label","status","done_av","total_av","total_all","started_at","finished_at","duration","message")
        self.jobs=ttk.Treeview(root,columns=jcols,show="headings",height=8)
        for c in jcols: self.jobs.heading(c,text=c)
        for name,w in zip(jcols,(40,160,90,80,80,90,150,150,100,360)): self.jobs.column(name,width=w,anchor="w")
        self.jobs.grid(row=r,column=0,columnspan=3,sticky=(N,S,E,W),padx=10); root.grid_rowconfigure(r,weight=1); r+=1

        Button(root,text="Delete Selected Job",command=self.del_job).grid(row=r,column=0,padx=10,pady=6,sticky=W)

        self.progress=ttk.Progressbar(root,orient="horizontal",length=820,mode="determinate"); self.progress.grid(row=r,column=1,padx=10,pady=10,sticky=W)
        self.log_var=StringVar(value="Ready."); Label(root,textvariable=self.log_var).grid(row=r,column=2,sticky=E,padx=10)

        self.refresh_all()

    # ======= Database Manager =======
    def show_db_path(self):
        messagebox.showinfo("Current DB", current_db_path())

    def switch_db(self):
        path = filedialog.askopenfilename(title="Select existing catalog DB", filetypes=[("SQLite DB","*.db"),("All","*.*")], initialdir=BASE_DIR)
        if not path: return
        try:
            init_catalog_db(path).close()
            set_current_db(path)
            self.cat_db = path
            messagebox.showinfo("Switch DB", f"Switched to:\n{path}")
            self.refresh_all(reset_header=True)
        except Exception as e:
            messagebox.showerror("Switch DB", str(e))

    def create_db(self):
        path = filedialog.asksaveasfilename(title="Create new catalog DB", defaultextension=".db",
                                            filetypes=[("SQLite DB","*.db")], initialdir=BASE_DIR,
                                            initialfile=f"catalog_{time.strftime('%Y%m%d_%H%M%S')}.db")
        if not path: return
        try:
            if os.path.exists(path): os.remove(path)
            init_catalog_db(path).close()
            set_current_db(path)
            self.cat_db = path
            messagebox.showinfo("Create DB", f"New DB created:\n{path}")
            self.refresh_all(reset_header=True)
        except Exception as e:
            messagebox.showerror("Create DB", str(e))

    def reset_db(self):
        if not messagebox.askyesno("Reset DB", "This will DROP ALL TABLES (drives, jobs) in the CURRENT DB.\nContinue?"):
            return
        try:
            drop_all_tables(self.cat_db)
            messagebox.showinfo("Reset DB", "Database reset to empty schema.")
            self.refresh_all()
        except Exception as e:
            messagebox.showerror("Reset DB", str(e))

    def delete_db_file(self):
        if not messagebox.askyesno("Delete DB file", "Delete the CURRENT DB file from disk?\n(You cannot undo this)"):
            return
        path = self.cat_db
        try:
            # On essaye de fermer en douceur (aucune connexion persistante ici)
            if os.path.exists(path):
                os.remove(path)
            # Recréer une DB propre au même endroit
            init_catalog_db(path).close()
            messagebox.showinfo("Delete DB", "DB file deleted and recreated empty.")
            self.refresh_all()
        except Exception as e:
            messagebox.showerror("Delete DB", str(e))

    def open_db_folder(self):
        try:
            os.startfile(str(Path(self.cat_db).parent))
        except Exception as e:
            messagebox.showerror("Open DB Folder", str(e))

    def reset_everything(self):
        if not messagebox.askyesno("Reset EVERYTHING", "This deletes CURRENT DB content AND ALL shards/*.db files.\nAre you absolutely sure?"):
            return
        try:
            # Reset DB
            drop_all_tables(self.cat_db)
            # Delete shard files
            if os.path.isdir(SHARDS_DIR):
                for p in Path(SHARDS_DIR).glob("*.db"):
                    try: p.unlink()
                    except: pass
            messagebox.showinfo("Reset EVERYTHING", "Catalog reset + shards cleared.")
            self.refresh_all()
        except Exception as e:
            messagebox.showerror("Reset EVERYTHING", str(e))

    # ======= Helpers & UI =======
    def choose_path(self):
        d=filedialog.askdirectory()
        if d: self.path_var.set(d)
    def set_status(self,txt:str):
        self.log_var.set(txt); self.root.update_idletasks()
    def _prog(self,d,t):
        self.progress["maximum"]=max(1,t); self.progress["value"]=d; self.root.update_idletasks()

    def on_sel(self,ev=None):
        sel=self.tree.selection()
        if not sel: return
        v=self.tree.item(sel[0])["values"]
        _id,label,mount,dtype,notes,*_=v
        self.label_var.set(str(label)); self.path_var.set(str(mount)); self.type_var.set(str(dtype)); self.notes_var.set(str(notes) if notes else "")
    def on_dbl(self,ev=None):
        self.on_sel(); self.set_status("Fields loaded. Edit then click 'Update Selected Drive Info'.")

    def refresh_drives(self):
        conn=init_catalog_db(self.cat_db); c=conn.cursor()
        self.tree.delete(*self.tree.get_children())
        for row in c.execute("""SELECT id,label,mount_path,drive_type,notes,serial,model,total_bytes,free_bytes,scanned_at
                                FROM drives ORDER BY scanned_at DESC"""):
            id_,label,mount,dtype,notes,serial,model,total,free,ts=row
            tgb=round((total or 0)/(1024**3),2) if total else ""; fgb=round((free or 0)/(1024**3),2) if free else ""
            self.tree.insert("",END,values=(id_,label,mount,dtype or "",notes or "",serial or "",model or "",tgb,fgb,ts))
        conn.close()

    def refresh_jobs(self):
        conn=init_catalog_db(self.cat_db); c=conn.cursor()
        self.jobs.delete(*self.jobs.get_children())
        for row in c.execute("""SELECT id,drive_label,status,COALESCE(done_files,0),COALESCE(total_av,total_files,0),
                                       COALESCE(total_all,0), started_at,finished_at,COALESCE(duration_sec,0),COALESCE(message,'')
                                FROM jobs ORDER BY id DESC LIMIT 200"""):
            id_,dl,st,done,tot_av,tot_all,sa,fa,dur,msg=row
            dur_txt = str(timedelta(seconds=int(dur))) if dur else ""
            self.jobs.insert("",END,values=(id_,dl,st,done,tot_av,tot_all,sa,fa,dur_txt,msg))
        conn.close()

    def refresh_all(self, reset_header:bool=False):
        if reset_header:
            # actualise la ligne qui montre "Current DB: ..."
            for w in self.root.grid_slaves(row=0, column=0):
                try: w.destroy()
                except: pass
            Label(self.root,text=f"Current DB: {self.cat_db}").grid(row=0,column=0,columnspan=3,sticky=W,padx=10,pady=(8,2))
        self.refresh_drives(); self.refresh_jobs()

    # Drive ops
    def del_drive(self):
        sel=self.tree.selection()
        if not sel:
            if self.jobs.selection():
                messagebox.showinfo("Select drive","You selected a job. Select a row in **Drives in catalog** to delete a drive, or use **Delete Selected Job** to delete a job.")
            else:
                messagebox.showinfo("Select drive","Please select a drive row first.")
            return
        row=self.tree.item(sel[0])["values"]; did,label=row[0],row[1]
        if not messagebox.askyesno("Confirm delete", f"Delete drive '{label}' from catalog (shard remains)?"): return
        conn=init_catalog_db(self.cat_db); conn.execute("DELETE FROM drives WHERE id=?", (did,)); conn.commit(); conn.close()
        self.refresh_drives(); self.set_status(f"Deleted {label} from catalog.")

    def open_shard_selected(self):
        sel=self.tree.selection()
        if not sel:
            messagebox.showinfo("Open shard","Select a drive first."); return
        label=self.tree.item(sel[0])["values"][1]
        sp=shard_path(label)
        if os.path.exists(sp): os.startfile(sp)
        else: messagebox.showinfo("Open shard", f"No shard file for drive '{label}'.")

    def update_drive(self):
        sel=self.tree.selection()
        if not sel:
            messagebox.showinfo("Select drive","Please select a drive row first."); return
        row=self.tree.item(sel[0])["values"]; did=row[0]
        new_label=self.label_var.get().strip(); new_mount=self.path_var.get().strip()
        new_type=self.type_var.get().strip(); new_notes=self.notes_var.get().strip()
        if not new_label or not new_mount:
            messagebox.showerror("Missing fields","Please fill Label and Mount Path in the inputs before updating."); return
        conn=init_catalog_db(self.cat_db)
        conn.execute("""UPDATE drives SET label=?, mount_path=?, drive_type=?, notes=? WHERE id=?""",
                     (new_label,new_mount,new_type,new_notes,did))
        conn.commit(); conn.close()
        save_category_if_new(new_type); self.refresh_drives(); self.set_status(f"Updated drive {new_label}.")

    # Job ops
    def del_job(self):
        sel=self.jobs.selection()
        if not sel:
            messagebox.showinfo("Select job","Please select a job row first."); return
        jrow=self.jobs.item(sel[0])["values"]; jid=jrow[0]
        if not messagebox.askyesno("Confirm", f"Delete job #{jid} from history?"): return
        conn=init_catalog_db(self.cat_db); conn.execute("DELETE FROM jobs WHERE id=?", (jid,)); conn.commit(); conn.close()
        self.refresh_jobs(); self.set_status(f"Deleted job #{jid}.")

    # Scan control
    def start_scan(self):
        p=self.path_var.get().strip(); l=self.label_var.get().strip(); t=self.type_var.get().strip(); n=self.notes_var.get().strip()
        if not p or not l: messagebox.showerror("Missing fields","Please set both Mount Path and Disk Label."); return
        self.stop_ev.clear(); self.pause_ev.clear()
        do_hash = bool(self.hash_av.get())
        def worker():
            try:
                self.refresh_drives()
                Scanner(self.cat_db, self.set_status,self._prog,self.pause_ev,self.stop_ev,do_hash_av=do_hash).scan_auto(l,p,t,n)
            except Exception as e:
                messagebox.showerror("Scan error", str(e))
            finally:
                self.refresh_all()
        threading.Thread(target=worker,daemon=True).start()

    def start_rescan(self):
        p=self.path_var.get().strip(); l=self.label_var.get().strip()
        if not p or not l: messagebox.showerror("Missing fields","Please set both Mount Path and Disk Label."); return
        sp=shard_path(l)
        try:
            if os.path.exists(sp): os.remove(sp)
        except Exception as e:
            messagebox.showwarning("Shard delete", f"Could not delete shard DB:\n{e}")
        self.start_scan()

    def pause(self): self.pause_ev.set(); self.set_status("Paused.")
    def resume(self): self.pause_ev.clear(); self.set_status("Resumed.")
    def stop(self):
        self.stop_ev.set()
        conn=init_catalog_db(self.cat_db)
        conn.execute("""UPDATE jobs SET status='Canceled', finished_at=? 
                        WHERE status='Running'""", (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
        conn.commit(); conn.close()
        self.set_status("Cancel requested..."); self.refresh_jobs()

    def export_catalog(self):
        ts=time.strftime("%Y%m%d_%H%M%S"); dst=Path(EXPORTS)/f"catalog_{ts}.db"; shutil.copy2(self.cat_db,dst)
        messagebox.showinfo("Export", f"Exported catalog to:\n{dst}")

    def compact_catalog(self):
        try:
            compact_db(self.cat_db)
            messagebox.showinfo("Compact DB", "Catalog DB compacted.")
        except Exception as e:
            messagebox.showwarning("Compact DB", f"Error: {e}")

# Entry
def safe_main():
    try:
        ensure_dirs(); ensure_categories()
        if not os.path.exists(current_db_path()):
            init_catalog_db(current_db_path()).close()
        root=Tk()
        try:
            style=ttk.Style()
            if "vista" in style.theme_names(): style.theme_use("vista")
        except Exception: pass
        App(root); root.mainloop()
    except Exception:
        tb=traceback.format_exc()
        try: messagebox.showerror("Startup error", tb)
        except Exception: print(tb)

if __name__=="__main__":
    safe_main()
