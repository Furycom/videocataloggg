"""Verify backup snapshots."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Dict, List

from core.db import connect

from .errors import BackupError, BackupVerificationError
from .logs import BackupLogger


def _load_manifest(path: Path) -> Dict[str, object]:
    if not path.exists():
        raise BackupVerificationError(f"manifest not found at {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quick_check(db_path: Path) -> None:
    conn = connect(db_path, read_only=True)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        if row and str(row[0]).lower() != "ok":
            raise BackupVerificationError(f"quick_check failed for {db_path}: {row[0]}")
    finally:
        conn.close()


def verify_backup(working_dir: Path, backup_id: str, *, logger: BackupLogger) -> Dict[str, object]:
    base = working_dir / "backups" / backup_id
    manifest_path = base / "manifest.json"
    manifest = _load_manifest(manifest_path)
    files = manifest.get("files", [])
    if not isinstance(files, list):
        raise BackupError("invalid manifest structure")

    verified_dbs: List[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise BackupError("invalid manifest entry")
        rel = entry.get("path")
        checksum = entry.get("sha256")
        expected_size = entry.get("bytes")
        if not isinstance(rel, str) or not isinstance(checksum, str):
            raise BackupError("manifest entry missing path/sha")
        source = base / "files" / rel
        if not source.exists():
            raise BackupVerificationError(f"missing snapshot file: {rel}")
        actual_size = source.stat().st_size
        if expected_size is not None and int(expected_size) != actual_size:
            raise BackupVerificationError(f"size mismatch for {rel}")
        actual_checksum = _sha256(source)
        if actual_checksum != checksum:
            raise BackupVerificationError(f"checksum mismatch for {rel}")
        if source.suffix == ".db":
            _quick_check(source)
            verified_dbs.append(rel)

    bundle = base / "bundle.zip"
    if bundle.exists():
        with zipfile.ZipFile(bundle, "r") as archive:
            if "manifest.json" not in archive.namelist():
                raise BackupVerificationError("bundle missing manifest")
            bundle_manifest = json.loads(archive.read("manifest.json"))
            if int(bundle_manifest.get("version", 0)) != int(manifest.get("version", 0)):
                raise BackupVerificationError("bundle manifest version mismatch")
            for entry in files:
                rel = entry.get("path")
                checksum = entry.get("sha256")
                size = int(entry.get("bytes", 0))
                if not isinstance(rel, str):
                    continue
                name = f"files/{rel}" if not rel.startswith("files/") else rel
                try:
                    data = archive.read(name)
                except KeyError as exc:
                    raise BackupVerificationError(f"bundle missing {rel}") from exc
                if hashlib.sha256(data).hexdigest() != checksum:
                    raise BackupVerificationError(f"bundle checksum mismatch for {rel}")
                if len(data) != size:
                    raise BackupVerificationError(f"bundle size mismatch for {rel}")

    logger.event(event="backup_verified", phase="verify", ok=True, id=backup_id)
    return {
        "id": backup_id,
        "verified_dbs": verified_dbs,
        "file_count": len(files),
    }


__all__ = ["verify_backup"]
