"""Inventory helpers for eco-IO scans.

Provides MIME detection, categorization helpers and a batched writer for the
lightweight inventory table.
"""
from __future__ import annotations

import mimetypes
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

try:  # pragma: no cover - optional dependency
    import magic  # type: ignore
except Exception:  # pragma: no cover - best-effort import guard
    magic = None  # type: ignore


_COMMON_EXTENSION_MIME: Dict[str, str] = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "mov": "video/quicktime",
    "wmv": "video/x-ms-wmv",
    "ts": "video/mp2t",
    "m2ts": "video/mp2t",
    "webm": "video/webm",
    "mpg": "video/mpeg",
    "mpeg": "video/mpeg",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "wma": "audio/x-ms-wma",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "alac": "audio/alac",
    "aiff": "audio/x-aiff",
    "ape": "audio/ape",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "svg": "image/svg+xml",
    "heic": "image/heic",
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
    "rtf": "application/rtf",
    "zip": "application/zip",
    "rar": "application/vnd.rar",
    "7z": "application/x-7z-compressed",
    "gz": "application/gzip",
    "bz2": "application/x-bzip2",
    "xz": "application/x-xz",
    "tar": "application/x-tar",
    "iso": "application/x-iso9660-image",
    "exe": "application/vnd.microsoft.portable-executable",
    "msi": "application/x-msi",
    "apk": "application/vnd.android.package-archive",
}

_CATEGORY_BY_EXTENSION: Dict[str, str] = {
    # Video
    "mp4": "video",
    "mkv": "video",
    "avi": "video",
    "mov": "video",
    "wmv": "video",
    "m4v": "video",
    "ts": "video",
    "m2ts": "video",
    "webm": "video",
    "mpg": "video",
    "mpeg": "video",
    "vob": "video",
    "flv": "video",
    "3gp": "video",
    "ogv": "video",
    "mts": "video",
    # Audio
    "mp3": "audio",
    "flac": "audio",
    "aac": "audio",
    "m4a": "audio",
    "wav": "audio",
    "wma": "audio",
    "ogg": "audio",
    "opus": "audio",
    "alac": "audio",
    "aiff": "audio",
    "ape": "audio",
    "dsf": "audio",
    "dff": "audio",
    # Images
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "gif": "image",
    "bmp": "image",
    "tiff": "image",
    "webp": "image",
    "heic": "image",
    # Documents
    "pdf": "document",
    "doc": "document",
    "docx": "document",
    "ppt": "document",
    "pptx": "document",
    "xls": "document",
    "xlsx": "document",
    "txt": "document",
    "rtf": "document",
    "odt": "document",
    "ods": "document",
    "odp": "document",
    # Archives
    "zip": "archive",
    "rar": "archive",
    "7z": "archive",
    "gz": "archive",
    "bz2": "archive",
    "xz": "archive",
    "tar": "archive",
    "iso": "archive",
    # Executables
    "exe": "executable",
    "msi": "executable",
    "apk": "executable",
    "bat": "executable",
    "cmd": "executable",
    "sh": "executable",
    "app": "executable",
    "pkg": "executable",
}

_MIME_CATEGORY_PREFIX: Dict[str, str] = {
    "video/": "video",
    "audio/": "audio",
    "image/": "image",
    "text/": "document",
    "application/pdf": "document",
    "application/msword": "document",
    "application/vnd.openxmlformats-officedocument": "document",
    "application/vnd.ms-powerpoint": "document",
    "application/vnd.ms-excel": "document",
    "application/vnd.android.package-archive": "executable",
    "application/x-dosexec": "executable",
    "application/x-msi": "executable",
    "application/x-sh": "executable",
    "application/zip": "archive",
    "application/x-7z-compressed": "archive",
    "application/x-rar": "archive",
    "application/x-rar-compressed": "archive",
    "application/x-tar": "archive",
    "application/gzip": "archive",
    "application/x-bzip2": "archive",
    "application/x-xz": "archive",
}


def _normalize_extension(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix


def detect_mime(path: str) -> Tuple[Optional[str], str]:
    """Return best-effort MIME type and normalized extension for *path*."""

    ext = _normalize_extension(path)
    mime: Optional[str] = None
    if magic is not None:
        try:
            # python-magic may raise OSError for permission issues; ignore errors.
            mime = magic.from_file(path, mime=True)  # type: ignore[attr-defined]
        except Exception:
            mime = None
    if mime is None:
        if ext in _COMMON_EXTENSION_MIME:
            mime = _COMMON_EXTENSION_MIME[ext]
        else:
            guess, _ = mimetypes.guess_type(path, strict=False)
            mime = guess
    return mime, ext


def categorize(mime: Optional[str], ext: str) -> str:
    """Return high-level category for the given MIME/extension."""

    ext = ext.lower()
    if ext in _CATEGORY_BY_EXTENSION:
        return _CATEGORY_BY_EXTENSION[ext]
    if mime:
        lower = mime.lower()
        if lower in _MIME_CATEGORY_PREFIX:
            return _MIME_CATEGORY_PREFIX[lower]
        for prefix, category in _MIME_CATEGORY_PREFIX.items():
            if prefix.endswith("/") and lower.startswith(prefix):
                return category
    return "other"


@dataclass
class InventoryRow:
    path: str
    size_bytes: int
    mtime_utc: str
    ext: Optional[str]
    mime: Optional[str]
    category: str
    drive_label: str
    drive_type: Optional[str]
    indexed_utc: str


class InventoryWriter:
    """Buffered writer that batches upserts into the inventory table."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        batch_size: int = 1000,
        flush_interval: float = 2.0,
    ) -> None:
        self._conn = connection
        self._batch: list[InventoryRow] = []
        self._batch_size = max(1, int(batch_size))
        self._flush_interval = max(0.5, float(flush_interval))
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self.total_written = 0

    def add(self, row: InventoryRow) -> None:
        with self._lock:
            self._batch.append(row)
            now = time.monotonic()
            if len(self._batch) >= self._batch_size or (now - self._last_flush) >= self._flush_interval:
                self._flush_locked()

    def flush(self, *, force: bool = False) -> None:
        with self._lock:
            if force:
                self._flush_locked()
            elif self._batch:
                now = time.monotonic()
                if (now - self._last_flush) >= self._flush_interval:
                    self._flush_locked()

    def close(self) -> None:
        self.flush(force=True)

    def _flush_locked(self) -> None:
        if not self._batch:
            self._last_flush = time.monotonic()
            return
        rows = [
            (
                item.path,
                int(item.size_bytes),
                item.mtime_utc,
                item.ext,
                item.mime,
                item.category,
                item.drive_label,
                item.drive_type,
                item.indexed_utc,
            )
            for item in self._batch
        ]
        cur = self._conn.cursor()
        cur.executemany(
            """
            INSERT INTO inventory(
                path, size_bytes, mtime_utc, ext, mime, category, drive_label, drive_type, indexed_utc
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                size_bytes=excluded.size_bytes,
                mtime_utc=excluded.mtime_utc,
                ext=excluded.ext,
                mime=excluded.mime,
                category=excluded.category,
                drive_label=excluded.drive_label,
                drive_type=excluded.drive_type,
                indexed_utc=excluded.indexed_utc
            """,
            rows,
        )
        self._conn.commit()
        self.total_written += len(rows)
        self._batch.clear()
        self._last_flush = time.monotonic()


__all__ = [
    "InventoryWriter",
    "InventoryRow",
    "categorize",
    "detect_mime",
]
