from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_fp_tmk (
    path TEXT PRIMARY KEY,
    tmk_version TEXT,
    duration_seconds REAL,
    sig_bytes BLOB NOT NULL,
    updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_fp_updated ON video_fp_tmk(updated_utc);

CREATE TABLE IF NOT EXISTS audio_fp_chroma (
    path TEXT PRIMARY KEY,
    chroma_version TEXT,
    duration_seconds REAL,
    fp TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audio_fp_updated ON audio_fp_chroma(updated_utc);

CREATE TABLE IF NOT EXISTS video_vhash (
    path TEXT PRIMARY KEY,
    vhash64 TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS duplicate_candidates (
    path_a TEXT NOT NULL,
    path_b TEXT NOT NULL,
    score REAL NOT NULL,
    reason TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    PRIMARY KEY(path_a, path_b, reason)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


class FingerprintStore:
    def __init__(self, db_path: Path | str, *, timeout: float = 30.0) -> None:
        self._path = Path(db_path)
        self._conn = sqlite3.connect(
            str(self._path), timeout=timeout, check_same_thread=False
        )
        ensure_schema(self._conn)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def upsert_video_signature(
        self,
        *,
        path: str,
        duration: Optional[float],
        signature: bytes,
        version: Optional[str],
    ) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO video_fp_tmk(path, tmk_version, duration_seconds, sig_bytes, updated_utc)
            VALUES(?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                tmk_version=excluded.tmk_version,
                duration_seconds=excluded.duration_seconds,
                sig_bytes=excluded.sig_bytes,
                updated_utc=excluded.updated_utc
            """,
            (path, version, duration, sqlite3.Binary(signature), now),
        )
        self._conn.commit()

    def upsert_audio_signature(
        self,
        *,
        path: str,
        duration: Optional[float],
        fingerprint: str,
        version: Optional[str],
    ) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO audio_fp_chroma(path, chroma_version, duration_seconds, fp, updated_utc)
            VALUES(?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                chroma_version=excluded.chroma_version,
                duration_seconds=excluded.duration_seconds,
                fp=excluded.fp,
                updated_utc=excluded.updated_utc
            """,
            (path, version, duration, fingerprint, now),
        )
        self._conn.commit()

    def upsert_video_vhash(self, *, path: str, vhash: str) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO video_vhash(path, vhash64, updated_utc)
            VALUES(?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                vhash64=excluded.vhash64,
                updated_utc=excluded.updated_utc
            """,
            (path, vhash, now),
        )
        self._conn.commit()

    def has_video_signature(self, path: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM video_fp_tmk WHERE path=?", (path,)
        )
        return cur.fetchone() is not None

    def has_audio_signature(self, path: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM audio_fp_chroma WHERE path=?", (path,)
        )
        return cur.fetchone() is not None

    def has_vhash(self, path: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM video_vhash WHERE path=?", (path,))
        return cur.fetchone() is not None

    def store_candidate(self, path_a: str, path_b: str, score: float, reason: str) -> None:
        if path_a == path_b:
            return
        if path_b < path_a:
            path_a, path_b = path_b, path_a
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO duplicate_candidates(path_a, path_b, score, reason, created_utc)
            VALUES(?,?,?,?,?)
            ON CONFLICT(path_a, path_b, reason) DO UPDATE SET
                score=max(score, excluded.score),
                created_utc=excluded.created_utc
            """,
            (path_a, path_b, float(score), reason, now),
        )
        self._conn.commit()

    def iter_video_signatures(self) -> Iterator[Tuple[str, bytes, Optional[float]]]:
        cur = self._conn.execute(
            "SELECT path, sig_bytes, duration_seconds FROM video_fp_tmk"
        )
        for path, blob, duration in cur.fetchall():
            yield path, blob, duration

    def iter_audio_fingerprints(self) -> Iterator[Tuple[str, str, Optional[float]]]:
        cur = self._conn.execute(
            "SELECT path, fp, duration_seconds FROM audio_fp_chroma"
        )
        for path, fp, duration in cur.fetchall():
            yield path, fp, duration

    def fetch_vhashes(self) -> dict[str, str]:
        cur = self._conn.execute("SELECT path, vhash64 FROM video_vhash")
        return {row[0]: row[1] for row in cur.fetchall()}


def _utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
