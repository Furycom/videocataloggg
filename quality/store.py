"""SQLite helpers for persisting video quality results."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional


@dataclass(slots=True)
class QualityRow:
    path: str
    container: Optional[str]
    duration_s: Optional[float]
    width: Optional[int]
    height: Optional[int]
    video_codec: Optional[str]
    video_bitrate_kbps: Optional[int]
    audio_codecs: Optional[str]
    audio_channels_max: Optional[int]
    audio_langs: Optional[str]
    subs_present: int
    subs_langs: Optional[str]
    score: Optional[int]
    reasons_json: str
    updated_utc: str


@dataclass(slots=True)
class QualityXrefRow:
    path: str
    tmdb_runtime_min: Optional[int]
    runtime_match: Optional[int]
    note: Optional[str]
    updated_utc: str


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS video_quality (
            path TEXT PRIMARY KEY,
            container TEXT,
            duration_s REAL,
            width INTEGER,
            height INTEGER,
            video_codec TEXT,
            video_bitrate_kbps INTEGER,
            audio_codecs TEXT,
            audio_channels_max INTEGER,
            audio_langs TEXT,
            subs_present INTEGER,
            subs_langs TEXT,
            score INTEGER,
            reasons_json TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS video_quality_xref (
            path TEXT PRIMARY KEY,
            tmdb_runtime_min INTEGER,
            runtime_match INTEGER,
            note TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )


class QualityStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_tables(self.conn)

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def upsert_rows(self, rows: Iterable[QualityRow]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        sql = (
            """
            INSERT INTO video_quality (
                path, container, duration_s, width, height, video_codec, video_bitrate_kbps,
                audio_codecs, audio_channels_max, audio_langs, subs_present, subs_langs,
                score, reasons_json, updated_utc
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            ON CONFLICT(path) DO UPDATE SET
                container=excluded.container,
                duration_s=excluded.duration_s,
                width=excluded.width,
                height=excluded.height,
                video_codec=excluded.video_codec,
                video_bitrate_kbps=excluded.video_bitrate_kbps,
                audio_codecs=excluded.audio_codecs,
                audio_channels_max=excluded.audio_channels_max,
                audio_langs=excluded.audio_langs,
                subs_present=excluded.subs_present,
                subs_langs=excluded.subs_langs,
                score=excluded.score,
                reasons_json=excluded.reasons_json,
                updated_utc=excluded.updated_utc
            """
        )
        params = [
            (
                row.path,
                row.container,
                row.duration_s,
                row.width,
                row.height,
                row.video_codec,
                row.video_bitrate_kbps,
                row.audio_codecs,
                row.audio_channels_max,
                row.audio_langs,
                row.subs_present,
                row.subs_langs,
                row.score,
                row.reasons_json,
                row.updated_utc,
            )
            for row in rows
        ]
        with self.conn:
            self.conn.executemany(sql, params)
        return len(rows)

    def upsert_xref(self, rows: Iterable[QualityXrefRow]) -> int:
        entries = list(rows)
        if not entries:
            return 0
        sql = (
            """
            INSERT INTO video_quality_xref (
                path, tmdb_runtime_min, runtime_match, note, updated_utc
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                tmdb_runtime_min=excluded.tmdb_runtime_min,
                runtime_match=excluded.runtime_match,
                note=excluded.note,
                updated_utc=excluded.updated_utc
            """
        )
        params = [
            (
                row.path,
                row.tmdb_runtime_min,
                row.runtime_match,
                row.note,
                row.updated_utc,
            )
            for row in entries
        ]
        with self.conn:
            self.conn.executemany(sql, params)
        return len(entries)


__all__ = [
    "QualityRow",
    "QualityStore",
    "QualityXrefRow",
    "ensure_tables",
]
