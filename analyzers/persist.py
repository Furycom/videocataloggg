"""Persistence helpers for lightweight feature vectors."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

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


def ensure_transcript_tables(connection: sqlite3.Connection) -> None:
    cur = connection.cursor()
    cur.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS transcripts USING fts5(
            path UNINDEXED,
            content,
            tokenize='unicode61'
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_meta (
            path TEXT PRIMARY KEY,
            updated_utc TEXT NOT NULL,
            language TEXT
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_transcript_meta_lang ON transcript_meta(language)")
    connection.commit()


def ensure_caption_tables(connection: sqlite3.Connection) -> None:
    cur = connection.cursor()
    cur.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS captions USING fts5(
            path UNINDEXED,
            content,
            tokenize='unicode61'
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS caption_meta (
            path TEXT PRIMARY KEY,
            updated_utc TEXT NOT NULL,
            model TEXT
        )
        """
    )
    connection.commit()


@dataclass
class TranscriptRecord:
    path: str
    content: str
    language: Optional[str]


@dataclass
class CaptionRecord:
    path: str
    content: str
    model: Optional[str]


class _BaseTextWriter:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        batch_size: int = 32,
    ) -> None:
        self._conn = connection
        self._batch_size = max(1, int(batch_size))
        self._rows: List[Tuple[str, str]] = []
        self._meta_rows: List[Tuple[str, str, Optional[str]]] = []
        self._lock = threading.Lock()
        self.total_written = 0

    def _table_names(self) -> Tuple[str, str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _meta_columns(self) -> Sequence[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def add(self, record: TranscriptRecord | CaptionRecord) -> None:
        content = (record.content or "").strip()
        if not content:
            return
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(record, TranscriptRecord):
            extra = record.language
        else:
            extra = record.model
        with self._lock:
            self._rows.append((record.path, content))
            self._meta_rows.append((record.path, timestamp, extra))
            if len(self._rows) >= self._batch_size:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        self.flush()

    def _flush_locked(self) -> None:
        if not self._rows:
            return
        table, meta = self._table_names()
        cur = self._conn.cursor()
        unique_paths = {path for path, _ in self._rows}
        cur.executemany(f"DELETE FROM {table} WHERE path=?", [(path,) for path in unique_paths])
        cur.executemany(f"INSERT INTO {table}(path, content) VALUES(?, ?)", self._rows)
        columns = ",".join(self._meta_columns())
        updates = ",".join(f"{col}=excluded.{col}" for col in self._meta_columns()[1:])
        cur.executemany(
            f"""
            INSERT INTO {meta}({columns}) VALUES(?,?,?)
            ON CONFLICT(path) DO UPDATE SET {updates}
            """,
            self._meta_rows,
        )
        self._conn.commit()
        self.total_written += len(self._rows)
        self._rows.clear()
        self._meta_rows.clear()


class TranscriptWriter(_BaseTextWriter):
    def _table_names(self) -> Tuple[str, str]:
        return "transcripts", "transcript_meta"

    def _meta_columns(self) -> Sequence[str]:
        return ("path", "updated_utc", "language")


class CaptionWriter(_BaseTextWriter):
    def _table_names(self) -> Tuple[str, str]:
        return "captions", "caption_meta"

    def _meta_columns(self) -> Sequence[str]:
        return ("path", "updated_utc", "model")
