"""GuessIt parsing helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from .types import GuessResult

try:  # pragma: no cover - optional dependency
    from guessit import guessit  # type: ignore
except Exception:  # pragma: no cover - gracefully degrade
    guessit = None  # type: ignore


def _normalize_guess(payload: Dict[str, object]) -> GuessResult:
    title = payload.get("title")
    year = payload.get("year")
    edition = payload.get("edition")
    normalized = GuessResult(
        title=str(title) if isinstance(title, str) else None,
        year=int(year) if isinstance(year, int) else None,
        edition=str(edition) if isinstance(edition, str) else None,
        raw=dict(payload),
    )
    return normalized


def parse_folder_name(folder_path: Path) -> GuessResult:
    if guessit is None:
        return GuessResult(raw={})
    try:
        result = guessit(folder_path.name, {"type": "movie"})
    except Exception:
        return GuessResult(raw={})
    return _normalize_guess(result)


def parse_video_name(video_path: Path) -> GuessResult:
    if guessit is None:
        return GuessResult(raw={})
    try:
        result = guessit(video_path.name)
    except Exception:
        return GuessResult(raw={})
    return _normalize_guess(result)


def parse_nfo_identifiers(path: Path) -> Dict[str, str]:
    identifiers: Dict[str, str] = {}
    try:
        data = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return identifiers
    lower = data.lower()
    if "imdb" in lower:
        for token in ("imdbid", "imdb_id", "imdb"):
            idx = lower.find(token)
            if idx == -1:
                continue
            snippet = lower[idx : idx + 128]
            digits = ""
            for char in snippet:
                if char.isdigit():
                    digits += char
                elif digits:
                    break
            if len(digits) >= 7:
                identifiers.setdefault("imdb_id", f"tt{digits}")
                break
    if "tmdb" in lower or "themoviedb" in lower:
        for token in ("tmdbid", "tmdb_id", "themoviedb"):
            idx = lower.find(token)
            if idx == -1:
                continue
            snippet = lower[idx : idx + 96]
            digits = ""
            for char in snippet:
                if char.isdigit():
                    digits += char
                elif digits:
                    break
            if digits:
                identifiers.setdefault("tmdb_id", digits)
                break
    return identifiers


__all__ = [
    "parse_folder_name",
    "parse_video_name",
    "parse_nfo_identifiers",
]
