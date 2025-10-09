"""TV review queue helpers and persistence."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Sequence

from .tv_types import TVConfidenceBreakdown

SERIES = "series"
SEASON = "season"
EPISODE = "episode"


def ensure_tv_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tv_series_profile (
            series_root TEXT PRIMARY KEY,
            show_title TEXT,
            show_year INTEGER,
            ids_json TEXT,
            seasons_found INTEGER,
            assets_json TEXT,
            issues_json TEXT,
            confidence REAL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tv_season_profile (
            series_root TEXT,
            season_path TEXT PRIMARY KEY,
            season_number INTEGER,
            episodes_found INTEGER,
            expected_episodes INTEGER,
            assets_json TEXT,
            issues_json TEXT,
            confidence REAL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tv_episode_profile (
            episode_path TEXT PRIMARY KEY,
            series_root TEXT,
            season_number INTEGER,
            episode_numbers_json TEXT,
            air_date TEXT,
            parsed_title TEXT,
            ids_json TEXT,
            subtitles_json TEXT,
            audio_langs_json TEXT,
            issues_json TEXT,
            confidence REAL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tv_review_queue (
            item_type TEXT,
            item_key TEXT PRIMARY KEY,
            confidence REAL,
            reasons_json TEXT,
            questions_json TEXT,
            created_utc TEXT NOT NULL
        )
        """
    )


def _utc_now() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


def upsert_series_profile(
    conn: sqlite3.Connection,
    *,
    series_root: str,
    title: Optional[str],
    year: Optional[int],
    ids: Optional[dict],
    seasons_found: int,
    assets: dict,
    issues: Sequence[str],
    confidence: float,
) -> None:
    conn.execute(
        """
        INSERT INTO tv_series_profile (
            series_root, show_title, show_year, ids_json, seasons_found, assets_json,
            issues_json, confidence, updated_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(series_root) DO UPDATE SET
            show_title=excluded.show_title,
            show_year=excluded.show_year,
            ids_json=excluded.ids_json,
            seasons_found=excluded.seasons_found,
            assets_json=excluded.assets_json,
            issues_json=excluded.issues_json,
            confidence=excluded.confidence,
            updated_utc=excluded.updated_utc
        """,
        (
            series_root,
            title,
            year,
            json.dumps(ids or {}),
            seasons_found,
            json.dumps(assets),
            json.dumps(list(issues)),
            confidence,
            _utc_now(),
        ),
    )


def upsert_season_profile(
    conn: sqlite3.Connection,
    *,
    series_root: str,
    season_path: str,
    season_number: Optional[int],
    episodes_found: int,
    expected_episodes: Optional[int],
    assets: dict,
    issues: Sequence[str],
    confidence: float,
) -> None:
    conn.execute(
        """
        INSERT INTO tv_season_profile (
            series_root, season_path, season_number, episodes_found, expected_episodes,
            assets_json, issues_json, confidence, updated_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(season_path) DO UPDATE SET
            series_root=excluded.series_root,
            season_number=excluded.season_number,
            episodes_found=excluded.episodes_found,
            expected_episodes=excluded.expected_episodes,
            assets_json=excluded.assets_json,
            issues_json=excluded.issues_json,
            confidence=excluded.confidence,
            updated_utc=excluded.updated_utc
        """,
        (
            series_root,
            season_path,
            season_number,
            episodes_found,
            expected_episodes,
            json.dumps(assets),
            json.dumps(list(issues)),
            confidence,
            _utc_now(),
        ),
    )


def upsert_episode_profile(
    conn: sqlite3.Connection,
    *,
    episode_path: str,
    series_root: str,
    season_number: Optional[int],
    episodes_json: Sequence[int],
    air_date: Optional[str],
    parsed_title: Optional[str],
    ids: Optional[dict],
    subtitles: Sequence[str],
    audio_languages: Sequence[str],
    issues: Sequence[str],
    confidence: float,
) -> None:
    conn.execute(
        """
        INSERT INTO tv_episode_profile (
            episode_path, series_root, season_number, episode_numbers_json, air_date,
            parsed_title, ids_json, subtitles_json, audio_langs_json, issues_json,
            confidence, updated_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(episode_path) DO UPDATE SET
            series_root=excluded.series_root,
            season_number=excluded.season_number,
            episode_numbers_json=excluded.episode_numbers_json,
            air_date=excluded.air_date,
            parsed_title=excluded.parsed_title,
            ids_json=excluded.ids_json,
            subtitles_json=excluded.subtitles_json,
            audio_langs_json=excluded.audio_langs_json,
            issues_json=excluded.issues_json,
            confidence=excluded.confidence,
            updated_utc=excluded.updated_utc
        """,
        (
            episode_path,
            series_root,
            season_number,
            json.dumps(list(episodes_json)),
            air_date,
            parsed_title,
            json.dumps(ids or {}),
            json.dumps(list(subtitles)),
            json.dumps(list(audio_languages)),
            json.dumps(list(issues)),
            confidence,
            _utc_now(),
        ),
    )


def enqueue_review_item(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    item_key: str,
    breakdown: TVConfidenceBreakdown,
    questions: Optional[Sequence[str]] = None,
) -> None:
    reasons_json = json.dumps(breakdown.reasons)
    questions_json = json.dumps(list(questions or []))
    conn.execute(
        """
        INSERT INTO tv_review_queue (item_type, item_key, confidence, reasons_json, questions_json, created_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET
            item_type=excluded.item_type,
            confidence=excluded.confidence,
            reasons_json=excluded.reasons_json,
            questions_json=excluded.questions_json,
            created_utc=excluded.created_utc
        """,
        (
            item_type,
            item_key,
            breakdown.confidence,
            reasons_json,
            questions_json,
            _utc_now(),
        ),
    )


def list_review_queue(
    conn: sqlite3.Connection,
    *,
    item_type: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Sequence[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM tv_review_queue"
    params = []
    if item_type:
        sql += " WHERE item_type = ?"
        params.append(item_type)
    sql += " ORDER BY confidence ASC, created_utc ASC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


__all__ = [
    "SERIES",
    "SEASON",
    "EPISODE",
    "ensure_tv_tables",
    "upsert_series_profile",
    "upsert_season_profile",
    "upsert_episode_profile",
    "enqueue_review_item",
    "list_review_queue",
]
