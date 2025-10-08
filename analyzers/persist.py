"""Persistence helpers for lightweight feature vectors."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List

import numpy as np


def ensure_features_table(connection: sqlite3.Connection) -> None:
    cur = connection.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS features (
            path TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            vec BLOB NOT NULL,
            dim INTEGER NOT NULL,
            frames_used INTEGER NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_features_kind ON features(kind)")
    connection.commit()


@dataclass
class FeatureRecord:
    path: str
    kind: str  # "image" or "video"
    vector: np.ndarray
    frames_used: int


class FeatureWriter:
    """Buffered SQLite writer for lightweight feature vectors."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        batch_size: int = 64,
    ) -> None:
        self._conn = connection
        self._batch: List[tuple] = []
        self._batch_size = max(1, int(batch_size))
        self._lock = threading.Lock()
        self.total_images = 0
        self.total_videos = 0
        self.total_dimensions = 0
        self._last_flush = time.monotonic()

    def add(self, record: FeatureRecord) -> None:
        vector = np.asarray(record.vector, dtype=np.float32)
        dim = int(vector.size)
        blob = sqlite3.Binary(vector.tobytes())
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        row = (record.path, record.kind, blob, dim, int(max(1, record.frames_used)), timestamp)
        with self._lock:
            self._batch.append(row)
            if record.kind == "image":
                self.total_images += 1
            elif record.kind == "video":
                self.total_videos += 1
            self.total_dimensions += dim
            if len(self._batch) >= self._batch_size or (time.monotonic() - self._last_flush) >= 2.0:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        self.flush()

    def _flush_locked(self) -> None:
        if not self._batch:
            self._last_flush = time.monotonic()
            return
        cur = self._conn.cursor()
        cur.executemany(
            """
            INSERT INTO features(path, kind, vec, dim, frames_used, updated_utc)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                kind=excluded.kind,
                vec=excluded.vec,
                dim=excluded.dim,
                frames_used=excluded.frames_used,
                updated_utc=excluded.updated_utc
            """,
            self._batch,
        )
        self._conn.commit()
        self._batch.clear()
        self._last_flush = time.monotonic()

    def average_dimension(self) -> int:
        total = self.total_images + self.total_videos
        if total <= 0:
            return 0
        return int(round(self.total_dimensions / total))
