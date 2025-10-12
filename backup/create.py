"""Create backup snapshots for VideoCatalog."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from api import __version__ as APP_VERSION

from core.db import backup_sqlite, connect
from core.paths import get_catalog_db_path, get_data_dir, get_logs_dir, get_shards_dir

from .errors import BackupError
from .logs import BackupLogger
from .types import BackupArtifact, BackupManifest, BackupOptions, BackupResult

_BACKUP_VERSION = 1
_LOG_TAIL_BYTES = 5 * 1024 * 1024
_MAX_THUMB_EXPORT_BYTES = 20 * 1024 * 1024


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _checkpoint_and_check(db_path: Path, *, logger: BackupLogger, integrity_timeout: float = 5.0) -> None:
    if not db_path.exists():
        return
    logger.info("checkpoint", path=str(db_path))
    conn = connect(db_path, read_only=False)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result = conn.execute("PRAGMA quick_check").fetchone()
        if result and str(result[0]).lower() != "ok":
            raise BackupError(f"quick_check failed for {db_path}: {result[0]}")
        if os.environ.get("VIDEOCATALOG_DEBUG_INTEGRITY"):
            conn.execute("PRAGMA busy_timeout=2000")
            start = time.monotonic()
            cursor = conn.execute("PRAGMA integrity_check")
            row = cursor.fetchone()
            duration = time.monotonic() - start
            logger.info("integrity_check", path=str(db_path), duration_ms=int(duration * 1000))
            if row and str(row[0]).lower() != "ok":
                raise BackupError(f"integrity_check failed for {db_path}: {row[0]}")
            if duration > integrity_timeout:
                logger.warning("integrity_check_timeout", path=str(db_path), duration_ms=int(duration * 1000))
    finally:
        conn.close()


def _sha256_for_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_sqlite(db_path: Path, dest: Path, *, relative: str, logger: BackupLogger) -> BackupArtifact:
    _ensure_parent(dest)
    logger.info("copy_sqlite", source=str(db_path), dest=str(dest))
    backup_sqlite(str(db_path), str(dest))
    size = dest.stat().st_size
    checksum = _sha256_for_path(dest)
    return BackupArtifact(path=dest, relative_path=relative, size_bytes=size, sha256=checksum)


def _copy_file(source: Path, dest: Path, *, relative: str, logger: BackupLogger) -> Optional[BackupArtifact]:
    if not source.exists() or not source.is_file():
        return None
    _ensure_parent(dest)
    shutil.copy2(source, dest)
    size = dest.stat().st_size
    checksum = _sha256_for_path(dest)
    logger.info("copy_file", source=str(source), dest=str(dest), size=size)
    return BackupArtifact(path=dest, relative_path=relative, size_bytes=size, sha256=checksum)


def _copy_logs_tail(log_dir: Path, dest_dir: Path, *, logger: BackupLogger) -> List[BackupArtifact]:
    artifacts: List[BackupArtifact] = []
    if not log_dir.exists():
        return artifacts
    for log_path in sorted(log_dir.glob("*")):
        if not log_path.is_file():
            continue
        relative = log_path.name
        output = dest_dir / relative
        _ensure_parent(output)
        size = log_path.stat().st_size
        if size > _LOG_TAIL_BYTES:
            with log_path.open("rb") as src, output.open("wb") as dst:
                src.seek(-_LOG_TAIL_BYTES, os.SEEK_END)
                shutil.copyfileobj(src, dst)
        else:
            shutil.copy2(log_path, output)
        checksum = _sha256_for_path(output)
        artifacts.append(
            BackupArtifact(
                path=output,
                relative_path=f"logs/{relative}",
                size_bytes=output.stat().st_size,
                sha256=checksum,
            )
        )
        logger.info("copy_log_tail", source=str(log_path), dest=str(output), size=output.stat().st_size)
    return artifacts


def _relative_to_working_dir(path: Path, working_dir: Path) -> str:
    try:
        return str(path.relative_to(working_dir))
    except ValueError:
        return path.name


def _collect_sqlite_targets(working_dir: Path) -> List[Path]:
    targets: List[Path] = []
    catalog = get_catalog_db_path(working_dir)
    if catalog.exists():
        targets.append(catalog)
    data_dir = get_data_dir(working_dir)
    orchestrator_db = data_dir / "orchestrator.db"
    if orchestrator_db.exists():
        targets.append(orchestrator_db)
    semantic_db = working_dir / "semantic_index.db"
    if semantic_db.exists():
        targets.append(semantic_db)
    shards_dir = get_shards_dir(working_dir)
    if shards_dir.exists():
        for shard in sorted(shards_dir.glob("*.db")):
            targets.append(shard)
    return targets


def _export_thumbnails(
    catalog_copy: Path,
    dest_dir: Path,
    *,
    logger: BackupLogger,
    cap_bytes: int = _MAX_THUMB_EXPORT_BYTES,
) -> Optional[BackupArtifact]:
    if not catalog_copy.exists():
        return None
    conn = sqlite3.connect(str(catalog_copy))
    try:
        def table_exists(name: str) -> bool:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            return bool(row)

        rows = []
        rows_cs = []
        total = 0
        if table_exists("video_thumbs"):
            cursor = conn.execute(
                "SELECT drive_label, path, width, height, format, image_blob FROM video_thumbs ORDER BY drive_label, path"
            )
            rows = cursor.fetchall()
            for row in rows:
                blob = row[5]
                if blob:
                    total += len(blob)
        else:
            logger.info("thumb_export_skipped", reason="no_table", table="video_thumbs")
        if table_exists("contact_sheets"):
            cursor_cs = conn.execute(
                "SELECT drive_label, path, format, width, height, frame_count, image_blob FROM contact_sheets ORDER BY drive_label, path"
            )
            rows_cs = cursor_cs.fetchall()
            for row in rows_cs:
                blob = row[6]
                if blob:
                    total += len(blob)
        else:
            logger.info("thumb_export_skipped", reason="no_table", table="contact_sheets")
        if not rows and not rows_cs:
            return None
        if total > cap_bytes:
            logger.warning("thumb_export_skipped", reason="cap", size=total)
            return None
        output = dest_dir / "thumbnails.jsonl"
        _ensure_parent(output)
        with output.open("w", encoding="utf-8") as handle:
            for row in rows:
                payload = {
                    "kind": "thumb",
                    "drive_label": row[0],
                    "path": row[1],
                    "width": row[2],
                    "height": row[3],
                    "format": row[4],
                    "image_base64": base64.b64encode(row[5]).decode("ascii") if row[5] else None,
                }
                handle.write(json.dumps(payload) + "\n")
            for row in rows_cs:
                payload = {
                    "kind": "contact_sheet",
                    "drive_label": row[0],
                    "path": row[1],
                    "format": row[2],
                    "width": row[3],
                    "height": row[4],
                    "frame_count": row[5],
                    "image_base64": base64.b64encode(row[6]).decode("ascii") if row[6] else None,
                }
                handle.write(json.dumps(payload) + "\n")
        size = output.stat().st_size
        checksum = _sha256_for_path(output)
        logger.info("thumb_export", size=size)
        return BackupArtifact(path=output, relative_path=str(output), size_bytes=size, sha256=checksum)
    finally:
        conn.close()


def _include_vector_dir(working_dir: Path, dest_dir: Path, *, logger: BackupLogger) -> List[BackupArtifact]:
    ann_dir = working_dir / "data" / "ann"
    artifacts: List[BackupArtifact] = []
    if not ann_dir.exists():
        return artifacts
    for item in sorted(ann_dir.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(working_dir)
        dest = dest_dir / rel
        copied = _copy_file(item, dest, relative=str(rel), logger=logger)
        if copied:
            artifacts.append(copied)
    return artifacts


def _write_manifest(path: Path, manifest: BackupManifest) -> None:
    data = {
        "version": manifest.version,
        "app_version": manifest.app_version,
        "created_utc": manifest.created_utc,
        "files": manifest.files,
        "options": manifest.options,
        "notes": manifest.notes,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)


def _bundle_directory(root: Path, bundle_path: Path) -> None:
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(root.rglob("*")):
            if item.is_dir() or item.resolve() == bundle_path.resolve():
                continue
            archive.write(item, item.relative_to(root).as_posix())


def _verify_bundle(bundle: Path, manifest: BackupManifest) -> None:
    with zipfile.ZipFile(bundle, "r") as archive:
        manifest_bytes = archive.read("manifest.json")
        parsed = json.loads(manifest_bytes)
        if int(parsed.get("version", 0)) != manifest.version:
            raise BackupError("manifest version mismatch inside bundle")
        for entry in manifest.files:
            rel = entry.get("path")
            if not isinstance(rel, str):
                raise BackupError("invalid manifest entry")
            name = f"files/{rel}" if not rel.startswith("files/") else rel
            try:
                data = archive.read(name)
            except KeyError as exc:
                raise BackupError(f"missing {rel} inside bundle") from exc
            checksum = hashlib.sha256(data).hexdigest()
            size = len(data)
            expected_sha = entry.get("sha256")
            expected_size = int(entry.get("bytes", size))
            if checksum != expected_sha:
                raise BackupError(f"checksum mismatch for {rel} inside bundle")
            if size != expected_size:
                raise BackupError(f"size mismatch for {rel} inside bundle")


def create_backup(
    working_dir: Path,
    *,
    logger: BackupLogger,
    options: BackupOptions,
) -> BackupResult:
    backup_id = _timestamp()
    backup_dir = working_dir / "backups" / backup_id
    files_dir = backup_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    logger.event(event="backup_start", phase="create", ok=True, id=backup_id)

    sqlite_targets = _collect_sqlite_targets(working_dir)
    artifacts: List[BackupArtifact] = []
    for target in sqlite_targets:
        _checkpoint_and_check(target, logger=logger)
        relative = _relative_to_working_dir(target, working_dir)
        dest = files_dir / relative
        artifact = _copy_sqlite(target, dest, relative=relative, logger=logger)
        artifacts.append(artifact)

    settings_path = working_dir / "settings.json"
    copied = _copy_file(settings_path, files_dir / "settings.json", relative="settings.json", logger=logger)
    if copied:
        artifacts.append(copied)

    cache_db = working_dir / "cache" / "tmdb_cache.json"
    copied_cache = _copy_file(
        cache_db,
        files_dir / "cache" / "tmdb_cache.json",
        relative="cache/tmdb_cache.json",
        logger=logger,
    )
    if copied_cache:
        artifacts.append(copied_cache)

    if options.include_vectors:
        artifacts.extend(_include_vector_dir(working_dir, files_dir, logger=logger))

    if options.include_thumbs:
        catalog_relative = _relative_to_working_dir(get_catalog_db_path(working_dir), working_dir)
        catalog_copy = files_dir / catalog_relative
        thumb_artifact = _export_thumbnails(catalog_copy, files_dir / "exports", logger=logger)
        if thumb_artifact:
            thumb_artifact.relative_path = _relative_to_working_dir(thumb_artifact.path, files_dir)
            artifacts.append(thumb_artifact)

    if options.include_logs_tail:
        artifacts.extend(_copy_logs_tail(get_logs_dir(working_dir), files_dir / "logs", logger=logger))

    manifest_entries: List[Dict[str, object]] = []
    total_size = 0
    for artifact in artifacts:
        relative = artifact.relative_path
        if relative.startswith("files/"):
            entry_path = relative[len("files/") :]
        else:
            entry_path = relative
        manifest_entries.append({
            "path": entry_path,
            "bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        })
        total_size += artifact.size_bytes

    manifest = BackupManifest(
        version=_BACKUP_VERSION,
        app_version=APP_VERSION,
        created_utc=_utcnow(),
        files=manifest_entries,
        options={
            "include_vectors": options.include_vectors,
            "include_thumbs": options.include_thumbs,
            "include_logs_tail": options.include_logs_tail,
        },
        notes=options.note,
    )
    manifest_path = backup_dir / "manifest.json"
    _write_manifest(manifest_path, manifest)
    bundle_path = backup_dir / "bundle.zip"
    _bundle_directory(backup_dir, bundle_path)
    _verify_bundle(bundle_path, manifest)

    logger.event(event="backup_complete", phase="create", ok=True, id=backup_id, size=total_size)

    return BackupResult(
        backup_id=backup_id,
        directory=backup_dir,
        manifest_path=manifest_path,
        bundle_path=bundle_path,
        artifacts=artifacts,
        manifest=manifest,
    )


__all__ = ["create_backup"]
