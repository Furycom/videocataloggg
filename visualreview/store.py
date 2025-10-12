"""Persistence helpers for visual review assets."""
from __future__ import annotations

import io
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.db import connect
from .pillow_support import (
    PillowImage,
    PillowUnavailableError,
    ensure_pillow,
    load_pillow_image,
)

LOGGER = logging.getLogger("videocatalog.visualreview.store")


@dataclass(slots=True)
class VisualReviewStoreConfig:
    """Configuration for :class:`VisualReviewStore`."""

    max_thumbnail_bytes: int = 600_000
    max_contact_sheet_bytes: int = 2_000_000
    thumbnail_retention: int = 800
    sheet_retention: int = 400
    max_db_blob_mb: int = 256


@dataclass(slots=True)
class StoredThumbnail:
    """Thumbnail payload fetched from the persistence layer."""

    format: str
    width: int
    height: int
    data: bytes


@dataclass(slots=True)
class StoredContactSheet:
    """Contact sheet payload fetched from the persistence layer."""

    format: str
    width: int
    height: int
    frame_count: int
    data: bytes


class VisualReviewStore:
    """SQLite-backed storage for thumbnails and contact sheets."""

    def __init__(
        self,
        shard_path: Path,
        *,
        config: Optional[VisualReviewStoreConfig] = None,
    ) -> None:
        self._path = Path(shard_path)
        self._config = config or VisualReviewStoreConfig()
        self._conn = connect(self._path, read_only=False, check_same_thread=False)
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------
    def __enter__(self) -> "VisualReviewStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------
    # Persistence API
    # ------------------------------------------------------------------
    def upsert_thumbnail(
        self,
        *,
        item_type: str,
        item_key: str,
        image: PillowImage,
        format: str,
        quality: int,
    ) -> bool:
        if not ensure_pillow(LOGGER):
            return False
        payload = self._encode_image(image, format=format, quality=quality)
        if payload is None:
            return False
        if len(payload) > self._config.max_thumbnail_bytes:
            LOGGER.debug(
                "Thumbnail for %s/%s exceeds cap (%s > %s bytes)",
                item_type,
                item_key,
                len(payload),
                self._config.max_thumbnail_bytes,
            )
            return False
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO video_thumbs (
                    item_type, item_key, width, height, format, image_blob, updated_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_type, item_key) DO UPDATE SET
                    width=excluded.width,
                    height=excluded.height,
                    format=excluded.format,
                    image_blob=excluded.image_blob,
                    updated_utc=excluded.updated_utc
                """,
                (
                    item_type,
                    item_key,
                    int(image.width),
                    int(image.height),
                    format.upper(),
                    sqlite3.Binary(payload),
                    now,
                ),
            )
        self._trim_table_by_count("video_thumbs", self._config.thumbnail_retention)
        self._trim_table_by_blob_budget("video_thumbs", "image_blob")
        return True

    def upsert_contact_sheet(
        self,
        *,
        item_type: str,
        item_key: str,
        data: bytes,
        format: str,
        width: int,
        height: int,
        frame_count: int,
    ) -> bool:
        if not data:
            return False
        if len(data) > self._config.max_contact_sheet_bytes:
            LOGGER.debug(
                "Contact sheet for %s/%s exceeds cap (%s > %s bytes)",
                item_type,
                item_key,
                len(data),
                self._config.max_contact_sheet_bytes,
            )
            return False
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO contact_sheets (
                    item_type, item_key, format, width, height, frame_count, image_blob, updated_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_type, item_key) DO UPDATE SET
                    format=excluded.format,
                    width=excluded.width,
                    height=excluded.height,
                    frame_count=excluded.frame_count,
                    image_blob=excluded.image_blob,
                    updated_utc=excluded.updated_utc
                """,
                (
                    item_type,
                    item_key,
                    format.upper(),
                    int(width),
                    int(height),
                    int(frame_count),
                    sqlite3.Binary(data),
                    now,
                ),
            )
        self._trim_table_by_count("contact_sheets", self._config.sheet_retention)
        self._trim_table_by_blob_budget("contact_sheets", "image_blob")
        return True

    def cleanup(self) -> None:
        self._trim_table_by_count("video_thumbs", self._config.thumbnail_retention)
        self._trim_table_by_count("contact_sheets", self._config.sheet_retention)
        self._trim_table_by_blob_budget("video_thumbs", "image_blob")
        self._trim_table_by_blob_budget("contact_sheets", "image_blob")

    def fetch_thumbnail(
        self,
        *,
        item_type: str,
        item_key: str,
    ) -> Optional[StoredThumbnail]:
        try:
            cursor = self._conn.execute(
                """
                SELECT format, width, height, image_blob
                FROM video_thumbs
                WHERE item_type = ? AND item_key = ?
                LIMIT 1
                """,
                (item_type, item_key),
            )
        except sqlite3.DatabaseError:
            return None
        row = cursor.fetchone()
        if not row:
            return None
        blob = row[3]
        if blob is None:
            return None
        data = bytes(blob)
        if not data:
            return None
        return StoredThumbnail(
            format=str(row[0] or ""),
            width=int(row[1] or 0),
            height=int(row[2] or 0),
            data=data,
        )

    def fetch_contact_sheet(
        self,
        *,
        item_type: str,
        item_key: str,
    ) -> Optional[StoredContactSheet]:
        try:
            cursor = self._conn.execute(
                """
                SELECT format, width, height, frame_count, image_blob
                FROM contact_sheets
                WHERE item_type = ? AND item_key = ?
                LIMIT 1
                """,
                (item_type, item_key),
            )
        except sqlite3.DatabaseError:
            return None
        row = cursor.fetchone()
        if not row:
            return None
        blob = row[4]
        if blob is None:
            return None
        data = bytes(blob)
        if not data:
            return None
        return StoredContactSheet(
            format=str(row[0] or ""),
            width=int(row[1] or 0),
            height=int(row[2] or 0),
            frame_count=int(row[3] or 0),
            data=data,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_tables(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS video_thumbs (
                    item_type TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    format TEXT NOT NULL,
                    image_blob BLOB NOT NULL,
                    updated_utc TEXT NOT NULL,
                    PRIMARY KEY (item_type, item_key)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_video_thumbs_updated
                ON video_thumbs(updated_utc)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_sheets (
                    item_type TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    format TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    frame_count INTEGER NOT NULL,
                    image_blob BLOB NOT NULL,
                    updated_utc TEXT NOT NULL,
                    PRIMARY KEY (item_type, item_key)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_contact_sheets_updated
                ON contact_sheets(updated_utc)
                """
            )

    def _trim_table_by_count(self, table: str, retain: int) -> None:
        if table not in {"video_thumbs", "contact_sheets"}:
            return
        if retain <= 0:
            return
        try:
            cursor = self._conn.execute(f"SELECT COUNT(*) FROM {table}")
        except sqlite3.DatabaseError:
            return
        row = cursor.fetchone()
        total = int(row[0]) if row else 0
        overflow = total - retain
        if overflow <= 0:
            return
        LOGGER.debug("Trimming %s rows from %s (retain=%s)", overflow, table, retain)
        with self._conn:
            self._conn.execute(
                f"""
                DELETE FROM {table}
                WHERE rowid IN (
                    SELECT rowid FROM {table}
                    ORDER BY updated_utc ASC, rowid ASC
                    LIMIT ?
                )
                """,
                (overflow,),
            )

    def _trim_table_by_blob_budget(self, table: str, blob_column: str) -> None:
        if table not in {"video_thumbs", "contact_sheets"}:
            return
        limit_mb = max(0, int(getattr(self._config, "max_db_blob_mb", 0)))
        if limit_mb <= 0:
            return
        try:
            cursor = self._conn.execute(
                f"SELECT COALESCE(SUM(LENGTH({blob_column})), 0) FROM {table}"
            )
        except sqlite3.DatabaseError:
            return
        row = cursor.fetchone()
        total_bytes = int(row[0]) if row else 0
        limit_bytes = limit_mb * 1024 * 1024
        overflow = total_bytes - limit_bytes
        if overflow <= 0:
            return
        LOGGER.debug(
            "Blob budget exceeded for %s (total=%s, limit=%s) — trimming",
            table,
            total_bytes,
            limit_bytes,
        )
        with self._conn:
            remaining = overflow
            while remaining > 0:
                rows = self._conn.execute(
                    f"""
                    SELECT rowid, COALESCE(LENGTH({blob_column}), 0) AS blob_size
                    FROM {table}
                    ORDER BY updated_utc ASC, rowid ASC
                    LIMIT 50
                    """
                ).fetchall()
                if not rows:
                    break
                rowids: list[int] = []
                reclaimed = 0
                for rowid, blob_size in rows:
                    rowids.append(int(rowid))
                    reclaimed += int(blob_size or 0)
                    if reclaimed >= remaining:
                        break
                if not rowids:
                    break
                placeholders = ",".join("?" for _ in rowids)
                self._conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN ({placeholders})",
                    rowids,
                )
                remaining = max(0, remaining - reclaimed)

    def _encode_image(self, image: PillowImage, *, format: str, quality: int) -> Optional[bytes]:
        try:
            pillow_image = load_pillow_image()
        except PillowUnavailableError:
            return None
        if not isinstance(image, pillow_image.Image):
            return None
        fmt = (format or "JPEG").upper()
        buffer = io.BytesIO()
        save_image = image
        if fmt in {"JPEG", "JPG"} and image.mode not in {"RGB", "L"}:
            save_image = image.convert("RGB")
        try:
            if fmt in {"JPEG", "JPG"}:
                save_image.save(
                    buffer,
                    format="JPEG",
                    quality=max(1, min(quality, 95)),
                    optimize=True,
                    progressive=True,
                )
            elif fmt == "WEBP":
                save_image.save(
                    buffer,
                    format="WEBP",
                    quality=max(1, min(quality, 100)),
                    method=6,
                )
            else:
                save_image.save(buffer, format=fmt)
        except OSError:
            return None
        return buffer.getvalue()


def _utc_now() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
