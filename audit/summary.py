"""Read-only helpers to compute audit metrics from the catalog database."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from core.settings import load_settings

LOGGER = logging.getLogger("videocatalog.audit.summary")


@dataclass(slots=True)
class ConfidenceBreakdown:
    total: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "total": int(self.total),
            "high": int(self.high),
            "medium": int(self.medium),
            "low": int(self.low),
        }


@dataclass(slots=True)
class ReviewBreakdown:
    movies: int = 0
    episodes: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {"movies": int(self.movies), "episodes": int(self.episodes)}


@dataclass(slots=True)
class AuditSummary:
    generated_utc: str
    movies: ConfidenceBreakdown = field(default_factory=ConfidenceBreakdown)
    episodes: ConfidenceBreakdown = field(default_factory=ConfidenceBreakdown)
    review_queue: ReviewBreakdown = field(default_factory=ReviewBreakdown)
    duplicate_pairs: int = 0
    unresolved_movies: int = 0
    unresolved_episodes: int = 0
    quality_flags: Optional[int] = None
    api_health: Dict[str, object] = field(default_factory=dict)
    drives: List[Dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "generated_utc": self.generated_utc,
            "movies": self.movies.as_dict(),
            "episodes": self.episodes.as_dict(),
            "review_queue": self.review_queue.as_dict(),
            "duplicate_pairs": int(self.duplicate_pairs),
            "unresolved_movies": int(self.unresolved_movies),
            "unresolved_episodes": int(self.unresolved_episodes),
        }
        if self.quality_flags is not None:
            data["quality_flags"] = int(self.quality_flags)
        if self.api_health:
            data["api_health"] = self.api_health
        if self.drives:
            data["drives"] = list(self.drives)
        return data


def _now_utc() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
    except sqlite3.DatabaseError as exc:  # pragma: no cover - defensive
        LOGGER.debug("PRAGMA table lookup failed for %s: %s", table, exc)
        return False
    return cur.fetchone() is not None


def _load_thresholds(working_dir: Path) -> Tuple[float, float]:
    settings = load_settings(working_dir)
    low = 0.50
    high = 0.80
    if isinstance(settings, dict):
        structure_cfg = settings.get("structure")
        if isinstance(structure_cfg, dict):
            try:
                low = float(structure_cfg.get("low_threshold", low))
            except (TypeError, ValueError):
                pass
            try:
                high = float(structure_cfg.get("high_threshold", high))
            except (TypeError, ValueError):
                pass
    if not math.isfinite(low):
        low = 0.50
    if not math.isfinite(high):
        high = 0.80
    if high <= low:
        # keep a safe margin to avoid empty buckets
        high = min(0.99, low + 0.25)
    return max(0.0, min(low, 1.0)), max(0.0, min(high, 1.0))


def _count(conn: sqlite3.Connection, query: str, params: Tuple[object, ...] = ()) -> int:
    cur = conn.execute(query, params)
    row = cur.fetchone()
    if not row:
        return 0
    value = row[0]
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _gentle_chunks(cursor: sqlite3.Cursor, chunk: int = 400, delay: float = 0.01) -> Iterable[sqlite3.Row]:
    while True:
        rows = cursor.fetchmany(chunk)
        if not rows:
            break
        for row in rows:
            yield row
        if delay:
            time.sleep(delay)


def _parse_json(value: Optional[str]) -> object:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _has_known_ids(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    ids = payload.get("ids") if isinstance(payload.get("ids"), dict) else payload
    if isinstance(ids, dict):
        for key in ("tmdb", "tmdb_id", "themoviedb", "imdb", "imdb_id", "tvdb", "tvdb_id"):
            val = ids.get(key)
            if isinstance(val, (str, int)) and str(val).strip():
                return True
    return False


def _collect_drive_rows(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    if not _table_exists(conn, "drive_binding"):
        return []
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT drive_label, volume_guid, marker_seen, marker_last_scan_utc, last_scan_utc
            FROM drive_binding
            ORDER BY drive_label COLLATE NOCASE
            """
        )
        rows = [
            {
                "drive_label": row.get("drive_label"),
                "volume_guid": row.get("volume_guid"),
                "marker_seen": int(row.get("marker_seen") or 0),
                "marker_last_scan_utc": row.get("marker_last_scan_utc"),
                "last_scan_utc": row.get("last_scan_utc"),
            }
            for row in cursor.fetchall()
        ]
        return rows
    except sqlite3.DatabaseError as exc:
        LOGGER.warning("drive_binding query failed: %s", exc)
        return []
    finally:
        conn.row_factory = None


def _collect_api_health(conn: sqlite3.Connection) -> Dict[str, object]:
    health: Dict[str, object] = {}
    if not _table_exists(conn, "api_log") and not _table_exists(conn, "api_quota"):
        return health
    conn.row_factory = sqlite3.Row
    try:
        if _table_exists(conn, "api_quota"):
            quota_rows = conn.execute(
                "SELECT * FROM api_quota ORDER BY updated_utc DESC LIMIT 20"
            ).fetchall()
            quota_payload: List[Dict[str, object]] = []
            for row in quota_rows:
                quota_payload.append({key: row[key] for key in row.keys()})
            if quota_payload:
                health["quota"] = quota_payload
        if _table_exists(conn, "api_log"):
            log_rows = conn.execute(
                "SELECT * FROM api_log ORDER BY created_utc DESC LIMIT 50"
            ).fetchall()
            providers: Dict[str, Dict[str, object]] = {}
            errors: Dict[str, List[Dict[str, object]]] = {}
            for row in log_rows:
                provider = str(row.get("provider") or "misc")
                entry = {key: row[key] for key in row.keys()}
                if row.get("level") in ("error", "critical", "warning"):
                    errors.setdefault(provider, []).append(entry)
                if provider not in providers:
                    providers[provider] = entry
            if providers:
                health["last_status"] = providers
            if errors:
                health["recent_errors"] = errors
    except sqlite3.DatabaseError as exc:
        LOGGER.warning("API health query failed: %s", exc)
    finally:
        conn.row_factory = None
    return health


def _count_unresolved_movies(conn: sqlite3.Connection, *, gentle_sleep: float) -> int:
    if not _table_exists(conn, "folder_profile"):
        return 0
    unresolved = 0
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT assets_json, source_signals_json FROM folder_profile")
        for row in _gentle_chunks(cursor, delay=gentle_sleep):
            assets = _parse_json(row["assets_json"])
            if _has_known_ids(assets):
                continue
            signals = _parse_json(row["source_signals_json"])
            if _has_known_ids(signals):
                continue
            unresolved += 1
    except sqlite3.DatabaseError as exc:
        LOGGER.warning("Failed to inspect folder_profile IDs: %s", exc)
    finally:
        conn.row_factory = None
    return unresolved


def _count_unresolved_episodes(conn: sqlite3.Connection, *, gentle_sleep: float) -> int:
    if not _table_exists(conn, "tv_episode_profile"):
        return 0
    unresolved = 0
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT ids_json FROM tv_episode_profile")
        for row in _gentle_chunks(cursor, delay=gentle_sleep):
            ids_payload = _parse_json(row["ids_json"])
            if _has_known_ids(ids_payload):
                continue
            unresolved += 1
    except sqlite3.DatabaseError as exc:
        LOGGER.warning("Failed to inspect tv_episode_profile IDs: %s", exc)
    finally:
        conn.row_factory = None
    return unresolved


def _count_quality_flags(conn: sqlite3.Connection, *, gentle_sleep: float) -> Optional[int]:
    if not _table_exists(conn, "video_quality"):
        return None
    flagged = 0
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT reasons_json, score FROM video_quality")
        for row in _gentle_chunks(cursor, delay=gentle_sleep):
            reasons = _parse_json(row["reasons_json"])
            has_reasons = False
            if isinstance(reasons, dict):
                has_reasons = any(bool(v) for v in reasons.values())
            elif isinstance(reasons, list):
                has_reasons = bool(reasons)
            if has_reasons:
                flagged += 1
                continue
            score = row["score"]
            try:
                score_val = int(score) if score is not None else None
            except (TypeError, ValueError):
                score_val = None
            if score_val is not None and score_val < 70:
                flagged += 1
    except sqlite3.DatabaseError as exc:
        LOGGER.warning("video_quality scan failed: %s", exc)
    finally:
        conn.row_factory = None
    return flagged


def gather_summary(
    conn: sqlite3.Connection,
    *,
    working_dir: Path,
    low_threshold: Optional[float] = None,
    high_threshold: Optional[float] = None,
    gentle_sleep: float = 0.01,
) -> AuditSummary:
    """Compute high-level audit metrics from the existing catalog tables."""

    low, high = _load_thresholds(working_dir)
    if low_threshold is not None:
        low = float(low_threshold)
    if high_threshold is not None:
        high = float(high_threshold)
    summary = AuditSummary(generated_utc=_now_utc())

    if _table_exists(conn, "folder_profile"):
        summary.movies.total = _count(conn, "SELECT COUNT(*) FROM folder_profile")
        summary.movies.high = _count(
            conn,
            "SELECT COUNT(*) FROM folder_profile WHERE confidence >= ?",
            (high,),
        )
        summary.movies.medium = _count(
            conn,
            "SELECT COUNT(*) FROM folder_profile WHERE confidence >= ? AND confidence < ?",
            (low, high),
        )
        summary.movies.low = _count(
            conn,
            "SELECT COUNT(*) FROM folder_profile WHERE confidence < ?",
            (low,),
        )
    else:
        LOGGER.info("folder_profile table missing; movie totals unavailable.")

    if _table_exists(conn, "tv_episode_profile"):
        summary.episodes.total = _count(conn, "SELECT COUNT(*) FROM tv_episode_profile")
        summary.episodes.high = _count(
            conn,
            "SELECT COUNT(*) FROM tv_episode_profile WHERE confidence >= ?",
            (high,),
        )
        summary.episodes.medium = _count(
            conn,
            (
                "SELECT COUNT(*) FROM tv_episode_profile "
                "WHERE confidence >= ? AND confidence < ?"
            ),
            (low, high),
        )
        summary.episodes.low = _count(
            conn,
            "SELECT COUNT(*) FROM tv_episode_profile WHERE confidence < ?",
            (low,),
        )
    else:
        LOGGER.info("tv_episode_profile table missing; episode totals unavailable.")

    if _table_exists(conn, "review_queue"):
        summary.review_queue.movies = _count(
            conn, "SELECT COUNT(*) FROM review_queue"
        )
    if _table_exists(conn, "tv_review_queue"):
        summary.review_queue.episodes = _count(
            conn,
            "SELECT COUNT(*) FROM tv_review_queue WHERE item_type='episode'",
        )

    if _table_exists(conn, "duplicate_candidates"):
        summary.duplicate_pairs = _count(
            conn, "SELECT COUNT(*) FROM duplicate_candidates"
        )
    else:
        LOGGER.info("duplicate_candidates table missing; duplicate summary skipped.")

    summary.unresolved_movies = _count_unresolved_movies(
        conn, gentle_sleep=gentle_sleep
    )
    summary.unresolved_episodes = _count_unresolved_episodes(
        conn, gentle_sleep=gentle_sleep
    )
    summary.quality_flags = _count_quality_flags(conn, gentle_sleep=gentle_sleep)
    summary.api_health = _collect_api_health(conn)
    summary.drives = _collect_drive_rows(conn)

    return summary
