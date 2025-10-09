"""GuessIt wrappers for TV series parsing."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .guess import guessit  # type: ignore[attr-defined]
from .guess import parse_nfo_identifiers
from .tv_types import TVEpisodeGuess


def _ensure_list(value: object) -> List[int]:
    if isinstance(value, list):
        result: List[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result
    if isinstance(value, int):
        return [value]
    try:
        return [int(value)]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []


def parse_episode(path: Path) -> TVEpisodeGuess:
    """Parse *path* using GuessIt returning a normalized episode payload."""

    if guessit is None:  # type: ignore[truthy-function]
        return TVEpisodeGuess(raw={})
    try:
        payload = guessit(path.name, {"type": "episode"})
    except Exception:
        return TVEpisodeGuess(raw={})
    episodes = _ensure_list(payload.get("episode"))
    absolute = _ensure_list(payload.get("absolute_episode"))
    season: Optional[int] = None
    season_value = payload.get("season")
    try:
        season = int(season_value)
    except (TypeError, ValueError):
        season = None
    air_date = None
    if "date" in payload:
        raw_date = payload.get("date")
        if isinstance(raw_date, date):
            air_date = raw_date
    title = payload.get("episode_title")
    if not isinstance(title, str):
        title = payload.get("title") if isinstance(payload.get("title"), str) else None
    series = payload.get("series") or payload.get("title")
    if not isinstance(series, str):
        series = None
    guess = TVEpisodeGuess(
        series=series,
        season=season,
        episodes=episodes,
        absolute_numbers=absolute,
        air_date=air_date,
        title=title if isinstance(title, str) else None,
        raw=dict(payload),
    )
    return guess


def parse_series_nfo(nfo_path: Path) -> Dict[str, str]:
    """Parse identifiers from a series-level .nfo file."""

    return parse_nfo_identifiers(nfo_path)


def parse_season_nfo(nfo_path: Path) -> Dict[str, str]:
    return parse_nfo_identifiers(nfo_path)


def parse_episode_nfo(nfo_path: Path) -> Dict[str, str]:
    return parse_nfo_identifiers(nfo_path)


def detect_subtitle_languages(subtitle_files: Iterable[Path]) -> List[str]:
    """Guess subtitle languages from filename suffixes."""

    langs: List[str] = []
    for path in subtitle_files:
        stem = path.stem
        parts = stem.split(".")
        if len(parts) < 2:
            continue
        language_token = parts[-1]
        if len(language_token) in {2, 3} and language_token.isalpha():
            langs.append(language_token.lower())
    return sorted(set(langs))


__all__ = [
    "parse_episode",
    "parse_series_nfo",
    "parse_season_nfo",
    "parse_episode_nfo",
    "detect_subtitle_languages",
]
