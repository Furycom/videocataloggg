"""Baseline snapshot helpers for VideoCatalog audit operations."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from .summary import AuditSummary

LOGGER = logging.getLogger("videocatalog.audit.baseline")


@dataclass(slots=True)
class BaselineRecord:
    id: int
    created_utc: str
    totals: Dict[str, int]
    hashes: Dict[str, Optional[str]]
    drive_label: Optional[str] = None
    volume_guid: Optional[str] = None
    file_path: Optional[Path] = None

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "id": self.id,
            "created_utc": self.created_utc,
            "totals": self.totals,
            "hashes": self.hashes,
        }
        if self.drive_label is not None:
            payload["drive_label"] = self.drive_label
        if self.volume_guid is not None:
            payload["volume_guid"] = self.volume_guid
        if self.file_path is not None:
            payload["file_path"] = str(self.file_path)
        return payload


@dataclass(slots=True)
class AuditDelta:
    added_videos: int = 0
    removed_videos: int = 0
    added_episodes: int = 0
    removed_episodes: int = 0
    new_low_confidence: int = 0
    resolved_low_confidence: int = 0
    new_low_confidence_episodes: int = 0
    resolved_low_confidence_episodes: int = 0
    new_duplicates: int = 0
    resolved_duplicates: int = 0
    new_quality_flags: int = 0
    resolved_quality_flags: int = 0
    changed_hashes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, int | Tuple[str, ...]]:
        return {
            "added_videos": int(self.added_videos),
            "removed_videos": int(self.removed_videos),
            "added_episodes": int(self.added_episodes),
            "removed_episodes": int(self.removed_episodes),
            "new_low_confidence": int(self.new_low_confidence),
            "resolved_low_confidence": int(self.resolved_low_confidence),
            "new_low_confidence_episodes": int(self.new_low_confidence_episodes),
            "resolved_low_confidence_episodes": int(self.resolved_low_confidence_episodes),
            "new_duplicates": int(self.new_duplicates),
            "resolved_duplicates": int(self.resolved_duplicates),
            "new_quality_flags": int(self.new_quality_flags),
            "resolved_quality_flags": int(self.resolved_quality_flags),
            "changed_hashes": tuple(self.changed_hashes),
        }


def _now_utc() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_baseline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drive_label TEXT,
            volume_guid TEXT,
            created_utc TEXT NOT NULL,
            totals_json TEXT NOT NULL,
            hashes_json TEXT NOT NULL
        )
        """
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _row_get(row: sqlite3.Row, key: str) -> Optional[object]:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _hash_query(
    conn: sqlite3.Connection,
    query: str,
    *,
    params: Tuple[object, ...] = (),
    gentle_sleep: float = 0.01,
) -> Optional[str]:
    try:
        cursor = conn.execute(query, params)
    except sqlite3.DatabaseError as exc:
        LOGGER.debug("Hash query failed (%s): %s", query, exc)
        return None
    digest = hashlib.md5()
    while True:
        rows = cursor.fetchmany(400)
        if not rows:
            break
        for row in rows:
            value = row[0]
            if value is None:
                continue
            digest.update(str(value).encode("utf-8", "surrogateescape"))
        if gentle_sleep:
            time.sleep(gentle_sleep)
    return digest.hexdigest()


def _compute_hashes(conn: sqlite3.Connection, *, gentle_sleep: float) -> Dict[str, Optional[str]]:
    hashes: Dict[str, Optional[str]] = {}
    if _table_exists(conn, "folder_profile"):
        hashes["movies"] = _hash_query(
            conn,
            "SELECT folder_path FROM folder_profile ORDER BY folder_path",
            gentle_sleep=gentle_sleep,
        )
    if _table_exists(conn, "tv_episode_profile"):
        hashes["episodes"] = _hash_query(
            conn,
            "SELECT episode_path FROM tv_episode_profile ORDER BY episode_path",
            gentle_sleep=gentle_sleep,
        )
    if _table_exists(conn, "duplicate_candidates"):
        hashes["duplicates"] = _hash_query(
            conn,
            "SELECT path_a || '|' || path_b FROM duplicate_candidates ORDER BY 1",
            gentle_sleep=gentle_sleep,
        )
    if _table_exists(conn, "review_queue"):
        hashes["review_queue"] = _hash_query(
            conn,
            "SELECT folder_path FROM review_queue ORDER BY folder_path",
            gentle_sleep=gentle_sleep,
        )
    if _table_exists(conn, "tv_review_queue"):
        hashes["tv_review_queue"] = _hash_query(
            conn,
            "SELECT item_key FROM tv_review_queue WHERE item_type='episode' ORDER BY item_key",
            gentle_sleep=gentle_sleep,
        )
    if _table_exists(conn, "video_quality"):
        hashes["quality_flags"] = _hash_query(
            conn,
            "SELECT path FROM video_quality WHERE TRIM(COALESCE(reasons_json,'')) != '' ORDER BY path",
            gentle_sleep=gentle_sleep,
        )
    return hashes


def _totals_from_summary(summary: AuditSummary) -> Dict[str, int]:
    totals = {
        "videos": int(summary.movies.total),
        "episodes": int(summary.episodes.total),
        "low_conf_movies": int(summary.review_queue.movies),
        "low_conf_episodes": int(summary.review_queue.episodes),
        "duplicates": int(summary.duplicate_pairs),
        "quality_flags": int(summary.quality_flags or 0),
        "unresolved_movies": int(summary.unresolved_movies),
        "unresolved_episodes": int(summary.unresolved_episodes),
    }
    return totals


def create_baseline(
    conn: sqlite3.Connection,
    *,
    summary: AuditSummary,
    export_dir: Optional[Path] = None,
    drive_label: Optional[str] = None,
    volume_guid: Optional[str] = None,
    gentle_sleep: float = 0.01,
) -> BaselineRecord:
    """Persist a new baseline snapshot and optionally write it to disk."""

    ensure_table(conn)
    totals = _totals_from_summary(summary)
    hashes = _compute_hashes(conn, gentle_sleep=gentle_sleep)
    created = _now_utc()
    payload_totals = json.dumps(totals)
    payload_hashes = json.dumps(hashes)
    cur = conn.execute(
        """
        INSERT INTO audit_baseline (drive_label, volume_guid, created_utc, totals_json, hashes_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (drive_label, volume_guid, created, payload_totals, payload_hashes),
    )
    baseline_id = int(cur.lastrowid or 0)
    record = BaselineRecord(
        id=baseline_id,
        created_utc=created,
        totals=totals,
        hashes=hashes,
        drive_label=drive_label,
        volume_guid=volume_guid,
    )
    if export_dir is not None:
        export_path = Path(export_dir) / "baseline.json"
        with export_path.open("w", encoding="utf-8") as handle:
            json.dump({"summary": summary.as_dict(), "baseline": record.as_dict()}, handle, indent=2)
        LOGGER.info("Baseline snapshot stored at %s", export_path)
        record.file_path = export_path
    return record


def _load_baseline_row(row: sqlite3.Row) -> BaselineRecord:
    totals_raw = _row_get(row, "totals_json") or _row_get(row, 4)
    hashes_raw = _row_get(row, "hashes_json") or _row_get(row, 5)
    try:
        totals = json.loads(totals_raw)
    except json.JSONDecodeError:
        totals = {}
    try:
        hashes = json.loads(hashes_raw)
    except json.JSONDecodeError:
        hashes = {}
    return BaselineRecord(
        id=int(_row_get(row, "id") or _row_get(row, 0) or 0),
        created_utc=str(_row_get(row, "created_utc") or _row_get(row, 3) or ""),
        totals={k: int(v) for k, v in totals.items() if isinstance(v, (int, float))},
        hashes={k: (str(v) if v is not None else None) for k, v in hashes.items()},
        drive_label=_row_get(row, "drive_label"),
        volume_guid=_row_get(row, "volume_guid"),
    )


def get_latest_baseline(conn: sqlite3.Connection) -> Optional[BaselineRecord]:
    ensure_table(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id, drive_label, volume_guid, created_utc, totals_json, hashes_json
        FROM audit_baseline
        ORDER BY datetime(created_utc) DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    conn.row_factory = None
    if row is None:
        return None
    return _load_baseline_row(row)


def compare_to_latest(
    conn: sqlite3.Connection,
    *,
    summary: AuditSummary,
    gentle_sleep: float = 0.01,
) -> Tuple[Optional[BaselineRecord], Optional[AuditDelta]]:
    ensure_table(conn)
    latest = get_latest_baseline(conn)
    if latest is None:
        return None, None
    baseline_hashes = latest.hashes
    current_hashes = _compute_hashes(conn, gentle_sleep=gentle_sleep)
    changed_keys = tuple(
        key
        for key, value in current_hashes.items()
        if baseline_hashes.get(key) != value
    )
    totals = latest.totals
    delta = AuditDelta()
    current = _totals_from_summary(summary)
    delta.added_videos = max(0, current["videos"] - totals.get("videos", 0))
    delta.removed_videos = max(0, totals.get("videos", 0) - current["videos"])
    delta.added_episodes = max(0, current["episodes"] - totals.get("episodes", 0))
    delta.removed_episodes = max(0, totals.get("episodes", 0) - current["episodes"])
    delta.new_low_confidence = max(0, current["low_conf_movies"] - totals.get("low_conf_movies", 0))
    delta.resolved_low_confidence = max(0, totals.get("low_conf_movies", 0) - current["low_conf_movies"])
    delta.new_low_confidence_episodes = max(
        0, current["low_conf_episodes"] - totals.get("low_conf_episodes", 0)
    )
    delta.resolved_low_confidence_episodes = max(
        0, totals.get("low_conf_episodes", 0) - current["low_conf_episodes"]
    )
    delta.new_duplicates = max(0, current["duplicates"] - totals.get("duplicates", 0))
    delta.resolved_duplicates = max(0, totals.get("duplicates", 0) - current["duplicates"])
    delta.new_quality_flags = max(
        0, current["quality_flags"] - totals.get("quality_flags", 0)
    )
    delta.resolved_quality_flags = max(
        0, totals.get("quality_flags", 0) - current["quality_flags"]
    )
    delta.changed_hashes = changed_keys
    return latest, delta
