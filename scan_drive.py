import os, sys, sqlite3, json, time, shutil, subprocess
from pathlib import Path
from typing import Optional
from tqdm import tqdm
from blake3 import blake3

VIDEO_EXTS = {'.mp4','.mkv','.avi','.mov','.wmv','.m4v','.ts','.m2ts','.webm','.mpg','.mpeg'}

def run(cmd:list[str]) -> tuple[int,str,str]:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
    out, err = p.communicate()
    return p.returncode, out, err

def mediainfo_json(file_path: str) -> Optional[dict]:
    code, out, err = run(["mediainfo", "--Output=JSON", file_path])
    if code == 0 and out.strip():
        try:
            return json.loads(out)
        except Exception:
            return None
    return None

def ffmpeg_verify(file_path: str) -> bool:
    if not any(file_path.lower().endswith(e) for e in VIDEO_EXTS):
        return True
    # -v error: only show errors; -xerror: stop on error
    code, out, err = run(["ffmpeg","-v","error","-xerror","-i",file_path,"-f","null","-","-nostdin"])
    return code == 0

def hash_blake3(file_path: str, chunk: int = 1024*1024) -> str:
    h = blake3()
    with open(file_path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b: break
            h.update(b)
    return h.hexdigest()

def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
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

def try_smart_overview() -> str:
    # Best-effort: capture smartctl --scan-open and a subset of PhysicalDrive outputs
    acc = {"scan": None, "details": []}
    code, out, err = run(["smartctl","--scan-open"])
    if code == 0: acc["scan"] = out
    for n in range(0,10):
        code, out, err = run(["smartctl","-i","-H","-A","-j", fr"\\.\PhysicalDrive{n}"])
        if code == 0 and out.strip():
            acc["details"].append(json.loads(out))
    return json.dumps(acc, ensure_ascii=False)

def scan_drive(label: str, mount_path: str, db_path: str):
    mount = Path(mount_path)
    if not mount.exists():
        print(f"[ERROR] Mount path not found: {mount_path}")
        sys.exit(2)

    total, used, free = shutil.disk_usage(mount)
    conn = init_db(db_path)
    c = conn.cursor()

    # SMART best-effort (won't fail the scan if smartctl missing)
    smart_blob = None
    try:
        code, _, _ = run(["smartctl","--version"])
        if code == 0:
            smart_blob = try_smart_overview()
    except Exception:
        smart_blob = None

    c.execute("""INSERT INTO drives(label, mount_path, total_bytes, free_bytes, smart_scan, scanned_at)
                 VALUES(?,?,?,?,?,datetime('now'))""",
              (label, str(mount.resolve()), int(total), int(free), smart_blob, ))
    conn.commit()

    file_paths = []
    for root, dirs, files in os.walk(mount):
        for f in files:
            file_paths.append(os.path.join(root, f))

    for fp in tqdm(file_paths, desc=f"Scanning {label}"):
        try:
            size = os.path.getsize(fp)
            hb3 = hash_blake3(fp)
            mi = mediainfo_json(fp)
            ok = ffmpeg_verify(fp)
            mtime = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(fp)))
            c.execute("""INSERT INTO files(drive_label, path, size_bytes, hash_blake3, media_json, integrity_ok, mtime_utc)
                         VALUES(?,?,?,?,?,?,?)""",
                      (label, fp, int(size), hb3, json.dumps(mi, ensure_ascii=False) if mi else None, int(ok), mtime))
        except Exception as e:
            # Keep scanning on errors
            c.execute("""INSERT INTO files(drive_label, path, size_bytes, hash_blake3, media_json, integrity_ok, mtime_utc)
                         VALUES(?,?,?,?,?,?,?)""",
                      (label, fp, None, None, json.dumps({"error": str(e)}, ensure_ascii=False), 0, None))
    conn.commit()
    conn.close()
    print(f"[OK] Scan complete for {label}. DB: {db_path}")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python scan_drive.py <LABEL> <MOUNT_PATH> <DB_PATH>")
        sys.exit(1)
    label, mount_path, db_path = sys.argv[1], sys.argv[2], sys.argv[3]
    scan_drive(label, mount_path, db_path)
