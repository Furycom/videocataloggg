"""Feature extraction helpers for calibrated confidence scoring."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Dict, Iterable, Mapping, Optional, Tuple


def _load_json(raw: Optional[str]) -> Mapping[str, object]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, Mapping) else {}


def _count_list(raw: Optional[str]) -> int:
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except Exception:
        return 0
    if isinstance(data, list):
        return len(data)
    return 0


def _bool(value: object) -> float:
    return 1.0 if bool(value) else 0.0


def _clip(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _signal(features: Mapping[str, object], key: str) -> float:
    try:
        return float(features.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


class FeatureExtractor:
    """Collects normalized feature vectors for catalog items."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def movie_features(self, folder_path: str) -> Optional[Dict[str, float]]:
        row = self.conn.execute(
            """
            SELECT folder_path, confidence, assets_json, issues_json, source_signals_json
            FROM folder_profile
            WHERE folder_path = ?
            """,
            (folder_path,),
        ).fetchone()
        if row is None:
            return None

        signals = _load_json(row["source_signals_json"])
        assets = _load_json(row["assets_json"])
        issue_count = float(_count_list(row["issues_json"]))

        has_nfo = _bool((assets or {}).get("nfo"))
        nfo_files = 0
        nfo_raw = (assets or {}).get("nfo_files")
        if isinstance(nfo_raw, Iterable):
            nfo_files = sum(1 for _ in nfo_raw)

        candidates = self.conn.execute(
            """
            SELECT score, source
            FROM folder_candidates
            WHERE folder_path = ?
            ORDER BY score DESC
            LIMIT 1
            """,
            (folder_path,),
        ).fetchone()
        top_score = 0.0
        top_source = "none"
        if candidates is not None:
            try:
                top_score = float(candidates["score"] or 0.0)
            except (TypeError, ValueError):
                top_score = 0.0
            if candidates["source"]:
                top_source = str(candidates["source"])

        features: Dict[str, float] = {
            "confidence_rule": _clip(float(row["confidence"] or 0.0), 0.0, 1.0),
            "signal_canon": _clip(_signal(signals, "canon"), 0.0, 1.0),
            "signal_nfo": _clip(_signal(signals, "nfo"), 0.0, 1.0),
            "signal_oshash": _clip(_signal(signals, "oshash"), 0.0, 1.0),
            "signal_name": _clip(_signal(signals, "name_match"), 0.0, 1.0),
            "signal_runtime": _clip(_signal(signals, "runtime"), 0.0, 1.0),
            "issue_count": float(issue_count),
            "has_nfo": has_nfo,
            "nfo_files": float(nfo_files),
            "candidate_score": _clip(top_score, 0.0, 1.0),
        }
        features[f"candidate_source={top_source}"] = 1.0
        return features

    def episode_features(self, episode_path: str) -> Optional[Dict[str, float]]:
        row = self.conn.execute(
            """
            SELECT episode_path,
                   confidence,
                   ids_json,
                   subtitles_json,
                   audio_langs_json,
                   issues_json,
                   episode_numbers_json
            FROM tv_episode_profile
            WHERE episode_path = ?
            """,
            (episode_path,),
        ).fetchone()
        if row is None:
            return None

        ids = _load_json(row["ids_json"])
        issue_count = float(_count_list(row["issues_json"]))

        subtitles = _count_list(row["subtitles_json"])
        audio = _count_list(row["audio_langs_json"])
        episode_numbers = _count_list(row["episode_numbers_json"])

        features: Dict[str, float] = {
            "confidence_rule": _clip(float(row["confidence"] or 0.0), 0.0, 1.0),
            "issue_count": issue_count,
            "subtitles_count": float(subtitles),
            "audio_langs": float(audio),
            "episode_numbers": float(episode_numbers),
            "has_tmdb": _bool((ids or {}).get("tmdb")),
            "has_imdb": _bool((ids or {}).get("imdb")),
            "has_tvdb": _bool((ids or {}).get("tvdb")),
        }
        return features

    def collect(self, path: str) -> Optional[Dict[str, float]]:
        """Collect features for a movie folder or TV episode path."""

        features = self.movie_features(path)
        if features is not None:
            features["item_type=movie"] = 1.0
            return features
        episode = self.episode_features(path)
        if episode is not None:
            episode["item_type=episode"] = 1.0
            return episode
        return None


__all__ = ["FeatureExtractor"]

