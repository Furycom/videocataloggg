"""Restore backups safely with automatic rollback."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from api import __version__ as APP_VERSION

from core.db import connect

from .errors import BackupError, BackupRestoreError
from .logs import BackupLogger


def _load_manifest(path: Path) -> Dict[str, object]:
    if not path.exists():
        raise BackupRestoreError("Backup manifest missing")
    return json.loads(path.read_text(encoding="utf-8"))
def _should_restore(path: str, *, include_settings: bool) -> bool:
    if path == "settings.json" and not include_settings:
        return False
    if path.startswith("logs/"):
        return False
    return True


def _quick_check(path: Path) -> None:
    conn = connect(path, read_only=True)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        if row and str(row[0]).lower() != "ok":
            raise BackupRestoreError(f"quick_check failed for {path}: {row[0]}")
    finally:
        conn.close()


def _copy_to(target: Path, source: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def restore_backup(
    working_dir: Path,
    backup_id: str,
    *,
    logger: BackupLogger,
    include_settings: bool = False,
) -> Dict[str, object]:
    base = working_dir / "backups" / backup_id
    if not base.exists():
        raise BackupRestoreError(f"Backup {backup_id} not found at {base}")
    manifest_path = base / "manifest.json"
    manifest = _load_manifest(manifest_path)
    version = str(manifest.get("app_version") or "")
    if version and version.split(".")[0] != APP_VERSION.split(".")[0]:
        raise BackupRestoreError(
            f"Snapshot targets app version {version}, current {APP_VERSION}. Run migrations before restoring."
        )

    files = manifest.get("files", [])
    if not isinstance(files, list):
        raise BackupError("Invalid manifest structure")

    safety_root = working_dir / "backups" / "_safety"
    safety_root.mkdir(parents=True, exist_ok=True)
    safety_dir = safety_root / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{backup_id}"
    safety_dir.mkdir(parents=True, exist_ok=True)

    sources: List[tuple[str, Path, Path]] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str):
            continue
        if not _should_restore(rel, include_settings=include_settings):
            continue
        source = base / "files" / rel
        target = working_dir / rel
        if not source.exists():
            raise BackupRestoreError(f"Missing snapshot file {rel}")
        sources.append((rel, source, target))

    safety_copies: List[tuple[str, Path, Path]] = []
    for rel, _, target in sources:
        if target.exists():
            backup_path = safety_dir / rel
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_path)
            safety_copies.append((rel, backup_path, target))

    try:
        for rel, source, target in sources:
            logger.info("restore_copy", path=rel)
            _copy_to(target, source)

        for rel, _, target in sources:
            if target.suffix == ".db" and target.exists():
                _quick_check(target)
    except Exception as exc:
        logger.error("restore_failed", id=backup_id, error=str(exc))
        for rel, backup_path, target in reversed(safety_copies):
            if backup_path.exists():
                _copy_to(target, backup_path)
        raise

    logger.event(event="backup_restored", phase="restore", ok=True, id=backup_id)
    return {
        "id": backup_id,
        "restored": [rel for rel, *_ in sources],
        "safety_dir": str(safety_dir),
    }


__all__ = ["restore_backup"]
