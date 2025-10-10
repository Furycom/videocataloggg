"""Persistence helpers for visual review assets."""
from __future__ import annotations

import io
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image

from core.db import connect

LOGGER = logging.getLogger("videocatalog.visualreview.store")


@dataclass(slots=True)
class VisualReviewStoreConfig:
    """Configuration for :class:`VisualReviewStore`."""

    max_thumbnail_bytes: int = 600_000
    max_contact_sheet_bytes: int = 2_000_000
    thumbnail_retention: int = 800
    sheet_retention: int = 400


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
        image: Image.Image,
        format: str,
        quality: int,
    ) -> bool:
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
                INSERT INTO review_thumbnails (
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
        self._trim_table("review_thumbnails", self._config.thumbnail_retention)
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
                INSERT INTO review_contact_sheets (
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
        self._trim_table("review_contact_sheets", self._config.sheet_retention)
        return True

    def cleanup(self) -> None:
        self._trim_table("review_thumbnails", self._config.thumbnail_retention)
        self._trim_table("review_contact_sheets", self._config.sheet_retention)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_tables(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_thumbnails (
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
                CREATE INDEX IF NOT EXISTS idx_review_thumbnails_updated
                ON review_thumbnails(updated_utc)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_contact_sheets (
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
                CREATE INDEX IF NOT EXISTS idx_review_contact_sheets_updated
                ON review_contact_sheets(updated_utc)
                """
            )

    def _trim_table(self, table: str, retain: int) -> None:
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

    def _encode_image(self, image: Image.Image, *, format: str, quality: int) -> Optional[bytes]:
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
