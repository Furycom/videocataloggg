"""Persistence helpers for document previews."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

from core.db import transaction

LOGGER = logging.getLogger("videocatalog.docpreview.store")

_PREVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs_preview (
    path TEXT PRIMARY KEY,
    doc_type TEXT,
    lang TEXT,
    pages_sampled INTEGER,
    chars_used INTEGER,
    summary TEXT,
    keywords TEXT,
    updated_utc TEXT NOT NULL
);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    path,
    summary,
    keywords,
    tokenize='porter'
);
"""

_DIAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs_diag (
    path TEXT PRIMARY KEY,
    is_scanned INTEGER,
    had_ocr INTEGER,
    notes TEXT,
    updated_utc TEXT NOT NULL
);
"""


@dataclass(slots=True)
class PreviewRow:
    path: str
    doc_type: str
    lang: Optional[str]
    pages_sampled: int
    chars_used: int
    summary: str
    keywords: Sequence[str]


@dataclass(slots=True)
class DiagRow:
    path: str
    is_scanned: bool
    had_ocr: bool
    notes: str


def ensure_tables(conn) -> None:
    cur = conn.cursor()
    cur.executescript(_PREVIEW_SCHEMA)
    cur.executescript(_FTS_SCHEMA)
    cur.executescript(_DIAG_SCHEMA)
    conn.commit()


class PreviewStore:
    def __init__(self, conn, *, batch_size: int = 25) -> None:
        self._conn = conn
        self._batch: List[tuple[PreviewRow, Optional[DiagRow]]] = []
        self._batch_size = max(1, int(batch_size))

    def add(self, preview: PreviewRow, diag: Optional[DiagRow]) -> None:
        self._batch.append((preview, diag))
        if len(self._batch) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._batch:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self._conn):
            cur = self._conn.cursor()
            for preview, diag in self._batch:
                keywords_text = (
                    ", ".join(preview.keywords)
                    if isinstance(preview.keywords, (list, tuple))
                    else str(preview.keywords)
                )
                cur.execute(
                    """
                    INSERT INTO docs_preview(path, doc_type, lang, pages_sampled, chars_used, summary, keywords, updated_utc)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        doc_type=excluded.doc_type,
                        lang=excluded.lang,
                        pages_sampled=excluded.pages_sampled,
                        chars_used=excluded.chars_used,
                        summary=excluded.summary,
                        keywords=excluded.keywords,
                        updated_utc=excluded.updated_utc
                    """,
                    (
                        preview.path,
                        preview.doc_type,
                        preview.lang,
                        int(preview.pages_sampled),
                        int(preview.chars_used),
                        preview.summary,
                        keywords_text,
                        now,
                    ),
                )
                cur.execute("DELETE FROM docs_fts WHERE path = ?", (preview.path,))
                cur.execute(
                    "INSERT INTO docs_fts(path, summary, keywords) VALUES(?,?,?)",
                    (preview.path, preview.summary, keywords_text),
                )
                if diag is not None:
                    cur.execute(
                        """
                        INSERT INTO docs_diag(path, is_scanned, had_ocr, notes, updated_utc)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(path) DO UPDATE SET
                            is_scanned=excluded.is_scanned,
                            had_ocr=excluded.had_ocr,
                            notes=excluded.notes,
                            updated_utc=excluded.updated_utc
                        """,
                        (
                            diag.path,
                            1 if diag.is_scanned else 0,
                            1 if diag.had_ocr else 0,
                            diag.notes,
                            now,
                        ),
                    )
        self._batch.clear()

    def close(self) -> None:
        self.flush()


def upsert_many(conn, rows: Iterable[tuple[PreviewRow, Optional[DiagRow]]]) -> None:
    store = PreviewStore(conn)
    for preview, diag in rows:
        store.add(preview, diag)
    store.flush()


__all__ = [
    "DiagRow",
    "PreviewRow",
    "PreviewStore",
    "ensure_tables",
    "upsert_many",
]
