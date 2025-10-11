"""SQLite helpers for the learning subsystem."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def _utc_now() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


def ensure_learning_tables(conn: sqlite3.Connection) -> None:
    """Create the learning tables if they are missing."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learn_examples (
            path TEXT PRIMARY KEY,
            label INTEGER NOT NULL,
            label_source TEXT,
            ts_utc TEXT NOT NULL,
            features_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learn_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            algo TEXT NOT NULL,
            calibrated TEXT NOT NULL,
            onnx_path TEXT,
            metrics_json TEXT NOT NULL,
            created_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learn_config (
            k INTEGER NOT NULL DEFAULT 5,
            min_labels INTEGER NOT NULL DEFAULT 200,
            retrain_every_labels INTEGER NOT NULL DEFAULT 100,
            class_weight TEXT NOT NULL DEFAULT 'balanced',
            active_topN INTEGER NOT NULL DEFAULT 200,
            active_strategy TEXT NOT NULL DEFAULT 'uncertainty_diversity'
        )
        """
    )


@dataclass(slots=True)
class LearningExample:
    path: str
    label: int
    label_source: str
    ts_utc: str
    features: Dict[str, float]


def insert_example(
    conn: sqlite3.Connection,
    *,
    path: str,
    label: int,
    label_source: str,
    features: Mapping[str, float],
    ts: Optional[str] = None,
) -> None:
    """Persist a labeled example without overwriting existing entries."""

    payload = json.dumps(dict(features))
    ts_value = ts or _utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO learn_examples(path, label, label_source, ts_utc, features_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (path, int(label), label_source, ts_value, payload),
    )


def load_examples(conn: sqlite3.Connection) -> List[LearningExample]:
    """Return all stored examples."""

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT path, label, label_source, ts_utc, features_json FROM learn_examples"
    ).fetchall()
    results: List[LearningExample] = []
    for row in rows:
        try:
            features = json.loads(row["features_json"])
            if not isinstance(features, dict):
                continue
        except Exception:
            continue
        parsed: Dict[str, float] = {}
        for key, value in features.items():
            try:
                parsed[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        results.append(
            LearningExample(
                path=row["path"],
                label=int(row["label"]),
                label_source=row["label_source"] or "",
                ts_utc=row["ts_utc"],
                features=parsed,
            )
        )
    return results


def count_examples(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM learn_examples")
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def latest_model_record(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM learn_models ORDER BY created_utc DESC, id DESC LIMIT 1"
    ).fetchone()


def record_model(
    conn: sqlite3.Connection,
    *,
    version: str,
    algo: str,
    calibrated: str,
    onnx_path: Optional[str],
    metrics: Mapping[str, object],
) -> None:
    conn.execute(
        """
        INSERT INTO learn_models(version, algo, calibrated, onnx_path, metrics_json, created_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            version,
            algo,
            calibrated,
            onnx_path,
            json.dumps(metrics),
            _utc_now(),
        ),
    )


def upsert_runtime_config(
    conn: sqlite3.Connection,
    *,
    k: int,
    min_labels: int,
    retrain_every_labels: int,
    class_weight: str,
    active_topn: int,
    active_strategy: str,
) -> None:
    conn.execute("DELETE FROM learn_config")
    conn.execute(
        """
        INSERT INTO learn_config(k, min_labels, retrain_every_labels, class_weight, active_topN, active_strategy)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (k, min_labels, retrain_every_labels, class_weight, active_topn, active_strategy),
    )


def load_runtime_config(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM learn_config LIMIT 1").fetchone()


__all__ = [
    "LearningExample",
    "count_examples",
    "ensure_learning_tables",
    "insert_example",
    "latest_model_record",
    "load_examples",
    "load_runtime_config",
    "record_model",
    "upsert_runtime_config",
]

