"""External verification helpers for TV series."""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence

import requests

try:  # pragma: no cover - optional dependency
    from rapidfuzz import fuzz  # type: ignore
except Exception:  # pragma: no cover - gracefully degrade
    fuzz = None  # type: ignore

try:  # pragma: no cover - optional dependency
    import tmdbsimple as tmdb  # type: ignore
except Exception:  # pragma: no cover - gracefully degrade
    tmdb = None  # type: ignore

try:  # pragma: no cover - optional dependency
    from imdb import Cinemagoer  # type: ignore
except Exception:  # pragma: no cover - gracefully degrade
    Cinemagoer = None  # type: ignore

from .tv_types import (
    TVEpisodeFile,
    TVEpisodeVerification,
    TVSeasonVerification,
    TVSeriesInfo,
    TVSeriesVerification,
    TVSettings,
    TVVerificationCandidate,
)
from .verify import compute_oshash_signature

LOGGER = logging.getLogger("videocatalog.tv.verify")

OPEN_SUBTITLES_URL = "https://api.opensubtitles.com/api/v1/subtitles"


def _score_against_names(name: Optional[str], names: Sequence[str]) -> float:
    if not name:
        return 0.0
    if not names:
        return 0.0
    if fuzz is None:
        return 0.0
    best = 0.0
    for candidate in names:
        try:
            score = float(fuzz.token_sort_ratio(name, candidate)) / 100.0
        except Exception:
            continue
        best = max(best, score)
    return best


@lru_cache(maxsize=1)
def _cinemagoer_client() -> Optional[Cinemagoer]:  # type: ignore[valid-type]
    if Cinemagoer is None:
        return None
    try:
        return Cinemagoer()
    except Exception:
        return None


def _tmdb_request(path: str, params: Dict[str, object], settings: TVSettings) -> Optional[Dict[str, object]]:
    api_key = settings.tmdb_api_key or os.environ.get("TMDB_API_KEY")
    if not api_key:
        LOGGER.debug("TMDb request skipped: missing api key")
        return None
    if tmdb is not None:
        tmdb.API_KEY = api_key  # type: ignore[attr-defined]
    url = f"https://api.themoviedb.org/3{path}"
    params = dict(params)
    params.setdefault("api_key", api_key)
    params.setdefault("language", settings.tmdb_lang)
    try:
        response = requests.get(url, params=params, timeout=settings.tmdb_timeout)
    except requests.RequestException as exc:
        LOGGER.debug("TMDb request failed: %s", exc)
        return None
    if response.status_code >= 300:
        LOGGER.debug("TMDb HTTP %s for %s", response.status_code, path)
        return None
    try:
        return response.json()
    except ValueError:
        LOGGER.debug("TMDb returned invalid JSON for %s", path)
        return None


def _tmdb_search_series(title: str, year: Optional[int], settings: TVSettings) -> List[Dict[str, object]]:
    params: Dict[str, object] = {"query": title}
    if year:
        params["first_air_date_year"] = year
    payload = _tmdb_request("/search/tv", params, settings)
    results = payload.get("results") if isinstance(payload, dict) else None
    if isinstance(results, list):
        return [result for result in results if isinstance(result, dict)]
    return []


def verify_series(series: TVSeriesInfo, *, settings: TVSettings, names: Iterable[str]) -> TVSeriesVerification:
    names_list = [series.root.name]
    names_list.extend(str(name) for name in names if name)
    if series.title:
        names_list.append(series.title)
    candidates: List[TVVerificationCandidate] = []
    if series.title:
        for entry in _tmdb_search_series(series.title, series.year, settings):
            title = entry.get("name") or entry.get("original_name")
            year = entry.get("first_air_date")
            first_year = None
            if isinstance(year, str) and len(year) >= 4 and year[:4].isdigit():
                first_year = int(year[:4])
            score = _score_against_names(str(title) if title else "", names_list)
            candidate = TVVerificationCandidate(
                source="tmdb",
                candidate_id=str(entry.get("id")) if entry.get("id") is not None else None,
                title=str(title) if title else None,
                year=first_year,
                score=score,
                extra={"vote_average": entry.get("vote_average"), "popularity": entry.get("popularity")},
            )
            candidates.append(candidate)
    if settings.imdb_enabled and series.title:
        client = _cinemagoer_client()
        if client is not None:
            try:
                results = client.search_movie(series.title)
            except Exception:
                results = []
            for item in results[: settings.max_candidates]:
                getter = item.get if hasattr(item, "get") else (lambda key, default=None: default)
                kind = getter("kind")
                if kind not in {"tv series", "tv mini series", "tv special"}:
                    continue
                imdb_id = None
                movie_id = getattr(item, "movieID", None)
                if movie_id:
                    imdb_id = f"tt{movie_id}"
                elif getter("movieID"):
                    imdb_id = f"tt{getter('movieID')}"
                year = getter("year")
                title = getter("title")
                score = _score_against_names(str(title) if title else "", names_list)
                candidates.append(
                    TVVerificationCandidate(
                        source="imdb",
                        candidate_id=imdb_id,
                        title=str(title) if title else None,
                        year=int(year) if isinstance(year, int) else None,
                        score=score,
                        extra={},
                    )
                )
    candidates.sort(key=lambda item: item.score, reverse=True)
    best = candidates[0] if candidates else None
    verification = TVSeriesVerification(candidates=candidates, name_score=best.score if best else 0.0)
    if best and best.source == "tmdb":
        verification.tmdb_id = best.candidate_id
    if best and best.source == "imdb":
        verification.imdb_id = best.candidate_id
    return verification


def verify_season(
    tmdb_id: Optional[str],
    season_number: Optional[int],
    *,
    settings: TVSettings,
) -> TVSeasonVerification:
    verification = TVSeasonVerification()
    if not tmdb_id or season_number is None:
        return verification
    payload = _tmdb_request(f"/tv/{tmdb_id}/season/{season_number}", {}, settings)
    if not isinstance(payload, dict):
        return verification
    episodes = payload.get("episodes")
    if isinstance(episodes, list):
        verification.expected_episodes = sum(1 for entry in episodes if isinstance(entry, dict))
    verification.tmdb_season_id = str(payload.get("id")) if payload.get("id") is not None else None
    return verification


def verify_episode(
    tmdb_id: Optional[str],
    season_number: Optional[int],
    episode_numbers: Sequence[int],
    episode_file: TVEpisodeFile,
    *,
    settings: TVSettings,
    names: Iterable[str],
) -> TVEpisodeVerification:
    verification = TVEpisodeVerification()
    if tmdb_id and season_number is not None and episode_numbers:
        payload = _tmdb_request(
            f"/tv/{tmdb_id}/season/{season_number}/episode/{episode_numbers[0]}",
            {},
            settings,
        )
        if isinstance(payload, dict):
            title = payload.get("name")
            score = _score_against_names(str(title) if title else "", list(names))
            verification.candidates.append(
                TVVerificationCandidate(
                    source="tmdb",
                    candidate_id=str(payload.get("id")) if payload.get("id") is not None else None,
                    title=str(title) if title else None,
                    year=None,
                    score=score,
                    extra={"air_date": payload.get("air_date")},
                )
            )
            verification.tmdb_episode_id = str(payload.get("id")) if payload.get("id") is not None else None
            verification.tmdb_air_date = payload.get("air_date") if isinstance(payload.get("air_date"), str) else None
    if settings.use_opensubtitles:
        oshash = compute_oshash_signature(
            episode_file.path,
            block_kib=settings.opensubtitles_read_kib,
        )
        if oshash:
            response = _opensubtitles_lookup(oshash, settings, names)
            if response:
                verification.oshash_match = response
                imdb_id = response.get("imdb_id")
                verification.candidates.append(
                    TVVerificationCandidate(
                        source="opensubs",
                        candidate_id=str(imdb_id) if imdb_id else None,
                        title=response.get("title"),
                        year=response.get("year") if isinstance(response.get("year"), int) else None,
                        score=float(response.get("score", 0.0)),
                        extra={"hash": oshash[0]},
                    )
                )
                if imdb_id:
                    verification.imdb_episode_id = str(imdb_id)
    verification.candidates.sort(key=lambda item: item.score, reverse=True)
    return verification


def _opensubtitles_lookup(signature: Optional[Sequence[object]], settings: TVSettings, names: Iterable[str]) -> Optional[Dict[str, object]]:
    if not signature:
        return None
    api_key = settings.opensubtitles_api_key or os.environ.get("OPENSUBTITLES_API_KEY")
    if not api_key:
        LOGGER.debug("OpenSubtitles lookup skipped: missing api key")
        return None
    token = signature[0]
    size = signature[1]
    params = {
        "moviehash": token,
        "moviebytesize": size,
    }
    headers = {
        "Api-Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "VideoCatalog/1.0 (tv verify)",
    }
    try:
        response = requests.get(OPEN_SUBTITLES_URL, params=params, headers=headers, timeout=settings.opensubtitles_timeout)
    except requests.RequestException as exc:
        LOGGER.debug("OpenSubtitles request failed: %s", exc)
        return None
    if response.status_code >= 300:
        LOGGER.debug("OpenSubtitles HTTP %s", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        LOGGER.debug("OpenSubtitles returned invalid JSON")
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    names_list = list(names)
    best_entry: Optional[Dict[str, object]] = None
    best_score = 0.0
    for entry in data:
        attributes = entry.get("attributes") if isinstance(entry, dict) else None
        if not isinstance(attributes, dict):
            continue
        feature = attributes.get("feature_details") if isinstance(attributes.get("feature_details"), dict) else None
        if not isinstance(feature, dict):
            feature = attributes.get("movie") if isinstance(attributes.get("movie"), dict) else None
        if not isinstance(feature, dict):
            continue
        title = feature.get("title") or feature.get("name")
        score = _score_against_names(str(title) if title else "", names_list)
        if score > best_score:
            best_score = score
            best_entry = {
                "imdb_id": feature.get("imdb_id") or feature.get("imdbid"),
                "title": title,
                "year": feature.get("year") or feature.get("release_year"),
                "score": score,
                "moviehash": token,
                "size": size,
            }
    return best_entry


__all__ = [
    "verify_series",
    "verify_season",
    "verify_episode",
]
