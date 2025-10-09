"""SQLite helpers for semantic indexing state."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

from core.db import connect

SEMANTIC_DB_FILENAME = "semantic_index.db"


def semantic_db_path(working_dir: Path) -> Path:
    return Path(working_dir) / SEMANTIC_DB_FILENAME


@contextmanager
def semantic_connection(working_dir: Path) -> Iterator[sqlite3.Connection]:
    path = semantic_db_path(working_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path, read_only=False, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drive_label TEXT NOT NULL,
            path TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding TEXT NOT NULL,
            dim INTEGER NOT NULL,
            embedding_norm REAL NOT NULL,
            kind TEXT,
            updated_utc TEXT,
            metadata TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_documents_path
        ON semantic_documents(drive_label, path)
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS semantic_documents_fts
        USING fts5(content, path UNINDEXED, drive_label UNINDEXED, tokenize = 'unicode61')
        """
    )


def upsert_document(
    conn: sqlite3.Connection,
    *,
    drive_label: str,
    path: str,
    content: str,
    embedding: Iterable[float],
    dim: int,
    embedding_norm: float,
    kind: Optional[str],
    updated_utc: Optional[str],
    metadata: Dict[str, Any],
) -> int:
    embedding_json = json.dumps(list(embedding))
    metadata_json = json.dumps(metadata, ensure_ascii=False)
    cursor = conn.execute(
        """
        SELECT id FROM semantic_documents
        WHERE drive_label = ? AND path = ?
        """,
        (drive_label, path),
    )
    row = cursor.fetchone()
    if row:
        doc_id = int(row["id"])
        conn.execute(
            """
            UPDATE semantic_documents
            SET content = ?, embedding = ?, dim = ?, embedding_norm = ?, kind = ?,
                updated_utc = ?, metadata = ?
            WHERE id = ?
            """,
            (
                content,
                embedding_json,
                dim,
                embedding_norm,
                kind,
                updated_utc,
                metadata_json,
                doc_id,
            ),
        )
        conn.execute(
            "DELETE FROM semantic_documents_fts WHERE rowid = ?",
            (doc_id,),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO semantic_documents (
                drive_label, path, content, embedding, dim, embedding_norm, kind, updated_utc, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                drive_label,
                path,
                content,
                embedding_json,
                dim,
                embedding_norm,
                kind,
                updated_utc,
                metadata_json,
            ),
        )
        doc_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO semantic_documents_fts(rowid, content, path, drive_label) VALUES (?, ?, ?, ?)",
        (doc_id, content, path, drive_label),
    )
    return doc_id


def delete_all(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM semantic_documents")
    conn.execute("DELETE FROM semantic_documents_fts")


def iter_documents(conn: sqlite3.Connection, *, drive_label: Optional[str] = None):
    sql = "SELECT * FROM semantic_documents"
    params: tuple[Any, ...] = ()
    if drive_label:
        sql += " WHERE drive_label = ?"
        params = (drive_label,)
    cursor = conn.execute(sql, params)
    for row in cursor.fetchall():
        yield row


def row_to_payload(row: sqlite3.Row) -> Dict[str, Any]:
    payload = {
        "id": int(row["id"]),
        "drive_label": row["drive_label"],
        "path": row["path"],
        "content": row["content"],
        "dim": int(row["dim"]),
        "embedding_norm": float(row["embedding_norm"] or 0.0),
        "kind": row["kind"],
        "updated_utc": row["updated_utc"],
    }
    try:
        payload["embedding"] = json.loads(row["embedding"] or "[]")
    except json.JSONDecodeError:
        payload["embedding"] = []
    try:
        payload["metadata"] = json.loads(row["metadata"] or "{}")
    except json.JSONDecodeError:
        payload["metadata"] = {}
    return payload
