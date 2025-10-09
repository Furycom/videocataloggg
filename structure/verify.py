"""External verification helpers for folder profiling."""
from __future__ import annotations

import json
import logging
import os
import struct
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, TYPE_CHECKING

from .types import GuessResult, VerificationCandidate, VerificationSignals, VideoFile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .service import StructureSettings

LOGGER = logging.getLogger("videocatalog.structure.verify")

try:  # pragma: no cover - optional dependency
    from rapidfuzz import fuzz  # type: ignore
except Exception:  # pragma: no cover - degrade gracefully
    fuzz = None  # type: ignore

try:  # pragma: no cover - optional dependency
    import tmdbsimple as tmdb  # type: ignore
except Exception:  # pragma: no cover - degrade gracefully
    tmdb = None  # type: ignore

try:  # pragma: no cover - optional dependency
    from imdb import Cinemagoer  # type: ignore
except Exception:  # pragma: no cover - degrade gracefully
    Cinemagoer = None  # type: ignore

try:  # pragma: no cover - optional dependency
    import requests  # type: ignore
except Exception:  # pragma: no cover - degrade gracefully
    requests = None  # type: ignore


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if fuzz is not None:
        try:
            return float(fuzz.token_sort_ratio(a, b)) / 100.0
        except Exception:
            return 0.0
    matcher = SequenceMatcher(None, a.lower(), b.lower())
    return matcher.ratio()


def _score_against_names(title: str, names: Sequence[str]) -> float:
    best = 0.0
    for name in names:
        score = _ratio(title, name)
        if score > best:
            best = score
    return best


def _tmdb_lookup(title: Optional[str], year: Optional[int], settings: "StructureSettings", names: Sequence[str]) -> List[VerificationCandidate]:
    if tmdb is None or not title:
        return []
    if not settings.tmdb_enabled:
        return []
    api_key = settings.tmdb_api_key or os.environ.get("TMDB_API_KEY")
    if not api_key:
        return []
    try:
        tmdb.API_KEY = api_key
    except Exception:
        tmdb.API_KEY = api_key  # type: ignore[attr-defined]
    try:
        search = tmdb.Search()  # type: ignore[operator]
        response = search.movie(query=title, year=year, include_adult=False)
    except Exception as exc:
        LOGGER.debug("TMDb lookup failed: %s", exc)
        return []
    results = []
    for item in response.get("results", [])[: settings.max_candidates]:
        candidate_title = item.get("title") or item.get("original_title")
        candidate_year = item.get("release_date", "")[:4]
        try:
            candidate_year_int = int(candidate_year)
        except Exception:
            candidate_year_int = None
        score = _score_against_names(str(candidate_title), names)
        results.append(
            VerificationCandidate(
                source="tmdb",
                candidate_id=str(item.get("id")) if item.get("id") is not None else None,
                title=str(candidate_title) if candidate_title else None,
                year=candidate_year_int,
                score=score,
                extra={
                    "popularity": item.get("popularity"),
                    "vote_average": item.get("vote_average"),
                },
            )
        )
    return results


def _imdb_lookup(title: Optional[str], year: Optional[int], settings: "StructureSettings", names: Sequence[str]) -> List[VerificationCandidate]:
    if Cinemagoer is None or not title:
        return []
    if not settings.imdb_enabled:
        return []
    try:
        client = Cinemagoer()
        search_results = client.search_movie(title)
    except Exception as exc:
        LOGGER.debug("IMDb lookup failed: %s", exc)
        return []
    results: List[VerificationCandidate] = []
    for entry in search_results[: settings.max_candidates]:
        candidate_title = entry.get("title")
        candidate_year = entry.get("year")
        score = _score_against_names(str(candidate_title), names)
        candidate_id = entry.movieID if hasattr(entry, "movieID") else None
        results.append(
            VerificationCandidate(
                source="imdb",
                candidate_id=f"tt{candidate_id}" if candidate_id else None,
                title=str(candidate_title) if candidate_title else None,
                year=int(candidate_year) if isinstance(candidate_year, int) else None,
                score=score,
                extra={},
            )
        )
    return results


def _chunk_sum(data: bytes) -> int:
    total = 0
    length = len(data)
    blocks = length // 8
    for idx in range(blocks):
        chunk = data[idx * 8 : idx * 8 + 8]
        total = (total + struct.unpack_from("<Q", chunk)[0]) & 0xFFFFFFFFFFFFFFFF
    remainder = length % 8
    if remainder:
        tail = data[-remainder:] + b"\x00" * (8 - remainder)
        total = (total + struct.unpack("<Q", tail)[0]) & 0xFFFFFFFFFFFFFFFF
    return total


def compute_oshash_signature(path: Path, *, block_kib: int = 64) -> Optional[Tuple[str, int]]:
    block_size = block_kib * 1024
    try:
        size = path.stat().st_size
    except OSError as exc:
        LOGGER.debug("Cannot stat %s: %s", path, exc)
        return None
    if size < block_size:
        block_size = int(size)
    try:
        with path.open("rb") as handle:
            head = handle.read(block_size)
            if size > block_size:
                handle.seek(max(0, size - block_size))
                tail = handle.read(block_size)
            else:
                tail = head
    except OSError as exc:
        LOGGER.debug("Cannot read %s for OSHash: %s", path, exc)
        return None
    total = (size + _chunk_sum(head) + _chunk_sum(tail)) & 0xFFFFFFFFFFFFFFFF
    return f"{total:016x}", size


def _opensubtitles_lookup(
    signature: Optional[Tuple[str, int]],
    settings: "StructureSettings",
    names: Sequence[str],
) -> Optional[Dict[str, object]]:
    if signature is None or not settings.opensubtitles_enabled:
        return None
    if requests is None:
        return None
    api_key = settings.opensubtitles_api_key or os.environ.get("OPENSUBTITLES_API_KEY")
    if not api_key:
        return None
    movie_hash, size = signature
    headers = {
        "Api-Key": api_key,
        "User-Agent": "VideoCatalogStructure/1.0",
    }
    params = {"moviehash": movie_hash, "moviebytesize": str(size)}
    try:
        response = requests.get(
            "https://api.opensubtitles.com/api/v1/subtitles",
            headers=headers,
            params=params,
            timeout=settings.opensubtitles_timeout,
        )
    except Exception as exc:
        LOGGER.debug("OpenSubtitles request failed: %s", exc)
        return None
    if response.status_code >= 300:
        LOGGER.debug("OpenSubtitles HTTP %s", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        LOGGER.debug("OpenSubtitles response was not JSON")
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    best_entry: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for entry in data:
        attributes = entry.get("attributes") if isinstance(entry, dict) else None
        if not isinstance(attributes, dict):
            continue
        movie = attributes.get("movie") if isinstance(attributes.get("movie"), dict) else attributes.get("feature_details")
        imdb_id = None
        title = None
        year = None
        if isinstance(movie, dict):
            imdb_id = movie.get("imdb_id") or movie.get("imdbid")
            title = movie.get("title") or movie.get("name")
            year = movie.get("year") or movie.get("release_year")
        score = _score_against_names(str(title) if title else "", names)
        if score > best_score:
            best_score = score
            best_entry = {
                "imdb_id": f"tt{imdb_id}" if imdb_id and not str(imdb_id).startswith("tt") else imdb_id,
                "title": title,
                "year": int(year) if isinstance(year, int) else year,
                "score": score,
                "moviehash": movie_hash,
                "size": size,
            }
    return best_entry


def collect_verification(
    *,
    folder_name: str,
    title_guess: GuessResult,
    video_guess: GuessResult,
    main_video: Optional[VideoFile],
    settings: "StructureSettings",
) -> VerificationSignals:
    names: List[str] = [folder_name]
    if main_video and main_video.basename:
        names.append(main_video.basename)
    candidates: List[VerificationCandidate] = []

    if title_guess.title:
        score = _score_against_names(title_guess.title, names)
        candidates.append(
            VerificationCandidate(
                source="name",
                candidate_id=None,
                title=title_guess.title,
                year=title_guess.year,
                score=score,
                extra={"from": "folder"},
            )
        )
    if video_guess.title and video_guess.title != title_guess.title:
        score = _score_against_names(video_guess.title, names)
        candidates.append(
            VerificationCandidate(
                source="name",
                candidate_id=None,
                title=video_guess.title,
                year=video_guess.year,
                score=score,
                extra={"from": "file"},
            )
        )

    candidates.extend(_tmdb_lookup(title_guess.title or video_guess.title, title_guess.year or video_guess.year, settings, names))
    candidates.extend(_imdb_lookup(title_guess.title or video_guess.title, title_guess.year or video_guess.year, settings, names))

    oshash = None
    if settings.opensubtitles_enabled and main_video is not None:
        signature = compute_oshash_signature(main_video.path, block_kib=settings.opensubtitles_read_kib)
        oshash = _opensubtitles_lookup(signature, settings, names)
        if oshash:
            title = oshash.get("title")
            score = _score_against_names(str(title) if title else "", names)
            candidates.append(
                VerificationCandidate(
                    source="opensubs",
                    candidate_id=str(ohash) if (ohash := oshash.get("imdb_id")) else None,
                    title=str(title) if title else None,
                    year=oshash.get("year") if isinstance(oshash.get("year"), int) else None,
                    score=score or float(oshash.get("score", 0.0) or 0.0),
                    extra={"hash": signature[0]} if signature else {},
                )
            )

    candidates.sort(key=lambda item: item.score, reverse=True)
    limited = candidates[: settings.max_candidates]
    best_name_score = max((cand.score for cand in limited), default=0.0)
    signals = VerificationSignals(candidates=limited, best_name_score=best_name_score, oshash_match=oshash)
    return signals


__all__ = [
    "collect_verification",
    "compute_oshash_signature",
]
