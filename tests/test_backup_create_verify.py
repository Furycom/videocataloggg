import json
import sqlite3
import zipfile
from pathlib import Path

from backup.create import create_backup
from backup.logs import BackupLogger
from backup.types import BackupOptions
from backup.verify import verify_backup


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS sample(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO sample(name) VALUES (?)", ("example",))
        conn.commit()
    finally:
        conn.close()


def test_create_and_verify_backup(tmp_path):
    working_dir = tmp_path / "working"
    (working_dir / "data").mkdir(parents=True)
    (working_dir / "logs").mkdir(parents=True)
    (working_dir / "cache").mkdir(parents=True)

    catalog_db = working_dir / "data" / "catalog.db"
    orchestrator_db = working_dir / "data" / "orchestrator.db"
    _init_db(catalog_db)
    _init_db(orchestrator_db)

    settings_path = working_dir / "settings.json"
    settings_path.write_text(json.dumps({"example": True}), encoding="utf-8")
    cache_path = working_dir / "cache" / "tmdb_cache.json"
    cache_path.write_text(json.dumps({"entries": {"movie": {"id": 1}}}), encoding="utf-8")

    logger = BackupLogger(working_dir)
    options = BackupOptions(include_logs_tail=False)
    result = create_backup(working_dir, logger=logger, options=options)

    manifest_path = result.manifest_path
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in manifest["files"]}
    assert "data/catalog.db" in paths
    assert "settings.json" in paths

    bundle_path = result.bundle_path
    assert bundle_path.exists()
    with zipfile.ZipFile(bundle_path, "r") as archive:
        assert "bundle.zip" not in archive.namelist()
        assert "manifest.json" in archive.namelist()
        assert "files/data/catalog.db" in archive.namelist()

    verify_info = verify_backup(working_dir, result.backup_id, logger=logger)
    assert verify_info["file_count"] == len(manifest["files"])
    assert "data/catalog.db" in verify_info["verified_dbs"]
