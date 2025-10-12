"""Checkpoint persistence helpers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_checkpoints (
            job_id INTEGER PRIMARY KEY,
            ckpt_json TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """
    )


def load_checkpoint(conn: sqlite3.Connection, job_id: int) -> Optional[Dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute("SELECT ckpt_json FROM job_checkpoints WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def save_checkpoint(conn: sqlite3.Connection, job_id: int, payload: Dict[str, Any]) -> None:
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO job_checkpoints (job_id, ckpt_json, updated_utc)
        VALUES (?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET ckpt_json=excluded.ckpt_json, updated_utc=excluded.updated_utc
        """,
        (job_id, json.dumps(payload, sort_keys=True), _utcnow()),
    )
    conn.commit()
