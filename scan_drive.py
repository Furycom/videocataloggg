import os, sys, sqlite3, json, time, shutil, subprocess, importlib
import importlib.util
from pathlib import Path
from typing import Optional
import hashlib

_tqdm_spec = importlib.util.find_spec("tqdm")
if _tqdm_spec is None:
    def tqdm(iterable, **kwargs):
        return iterable
else:
    tqdm = importlib.import_module("tqdm").tqdm

_blake3_spec = importlib.util.find_spec("blake3")
_blake3_hash = importlib.import_module("blake3").blake3 if _blake3_spec is not None else None


def _has_cmd(cmd: str) -> bool:
    """Return True if *cmd* is available on PATH."""
    return shutil.which(cmd) is not None


_ffmpeg_available = _has_cmd("ffmpeg")
_mediainfo_available = _has_cmd("mediainfo")

VIDEO_EXTS = {'.mp4','.mkv','.avi','.mov','.wmv','.m4v','.ts','.m2ts','.webm','.mpg','.mpeg'}

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

def mediainfo_json(file_path: str) -> Optional[dict]:
    global _mediainfo_available
    if not _mediainfo_available:
        return None
    code, out, err = run(["mediainfo", "--Output=JSON", file_path])
    if code == 0 and out.strip():
        try:
            return json.loads(out)
        except Exception:
            return None
    if code == 127:
        # Command missing – treat as unavailable for the remainder of the scan
        _mediainfo_available = False
    return None

def ffmpeg_verify(file_path: str) -> bool:
    if not any(file_path.lower().endswith(e) for e in VIDEO_EXTS):
        return True
    if not _ffmpeg_available:
        # ffmpeg absent: skip verification instead of marking files as invalid
        return True
    # -v error: only show errors; -xerror: stop on error
    code, out, err = run(["ffmpeg","-v","error","-xerror","-i",file_path,"-f","null","-","-nostdin"])
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

def scan_drive(label: str, mount_path: str, db_path: str, debug_slow: bool = False):
    global _ffmpeg_available, _mediainfo_available
    mount = Path(mount_path)
    if not mount.exists():
        print(f"[ERROR] Mount path not found: {mount_path}")
        sys.exit(2)

    _mediainfo_available = _has_cmd("mediainfo")
    _ffmpeg_available = _has_cmd("ffmpeg")
    missing_tools = []
    if not _mediainfo_available:
        missing_tools.append("mediainfo")
    if not _ffmpeg_available:
        missing_tools.append("ffmpeg")
    if missing_tools:
        for tool in missing_tools:
            print(json.dumps({"type": "tool_missing", "tool": tool}), flush=True)
        print(
            f"[ERROR] Missing required tool(s): {', '.join(missing_tools)}",
            file=sys.stderr,
        )
        sys.exit(3)

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
            if debug_slow:
                time.sleep(0.01)

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
    args = sys.argv[1:]
    debug_flag = False
    if "--debug-slow-enumeration" in args:
        args.remove("--debug-slow-enumeration")
        debug_flag = True
    if len(args) != 3:
        print("Usage: python scan_drive.py <LABEL> <MOUNT_PATH> <DB_PATH>")
        sys.exit(1)
    label, mount_path, db_path = args
    scan_drive(label, mount_path, db_path, debug_slow=debug_flag)
