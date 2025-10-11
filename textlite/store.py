"""Persistence helpers for TextLite previews."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from core.db import transaction

LOGGER = logging.getLogger("videocatalog.textlite.store")

_PREVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS textlite_preview (
    path TEXT PRIMARY KEY,
    kind TEXT,
    bytes_sampled INTEGER,
    lines_sampled INTEGER,
    summary TEXT,
    keywords TEXT,
    schema_json TEXT,
    updated_utc TEXT NOT NULL
);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS textlite_fts USING fts5(
    path,
    summary,
    keywords,
    schema,
    tokenize='porter'
);
"""


@dataclass(slots=True)
class PreviewRow:
    path: str
    kind: str
    bytes_sampled: int
    lines_sampled: int
    summary: str
    keywords: List[str]
    schema_json: Optional[str]


def ensure_tables(conn) -> None:
    cur = conn.cursor()
    cur.executescript(_PREVIEW_SCHEMA)
    cur.executescript(_FTS_SCHEMA)
    conn.commit()


class PreviewStore:
    def __init__(self, conn, *, batch_size: int = 32) -> None:
        self._conn = conn
        self._batch: List[PreviewRow] = []
        self._batch_size = max(1, int(batch_size))

    def add(self, preview: PreviewRow) -> None:
        self._batch.append(preview)
        if len(self._batch) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._batch:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self._conn):
            cur = self._conn.cursor()
            for preview in self._batch:
                keywords_text = json.dumps(preview.keywords, ensure_ascii=False)
                cur.execute(
                    """
                    INSERT INTO textlite_preview(path, kind, bytes_sampled, lines_sampled, summary, keywords, schema_json, updated_utc)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        kind=excluded.kind,
                        bytes_sampled=excluded.bytes_sampled,
                        lines_sampled=excluded.lines_sampled,
                        summary=excluded.summary,
                        keywords=excluded.keywords,
                        schema_json=excluded.schema_json,
                        updated_utc=excluded.updated_utc
                    """,
                    (
                        preview.path,
                        preview.kind,
                        int(preview.bytes_sampled),
                        int(preview.lines_sampled),
                        preview.summary,
                        keywords_text,
                        preview.schema_json,
                        now,
                    ),
                )
                cur.execute("DELETE FROM textlite_fts WHERE path = ?", (preview.path,))
                cur.execute(
                    "INSERT INTO textlite_fts(path, summary, keywords, schema) VALUES(?,?,?,?)",
                    (
                        preview.path,
                        preview.summary,
                        keywords_text,
                        preview.schema_json or "",
                    ),
                )
        self._batch.clear()

    def close(self) -> None:
        self.flush()


def upsert_many(conn, rows: Iterable[PreviewRow]) -> None:
    store = PreviewStore(conn)
    for preview in rows:
        store.add(preview)
    store.flush()


__all__ = ["PreviewRow", "PreviewStore", "ensure_tables", "upsert_many"]
