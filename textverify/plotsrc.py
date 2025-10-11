"""Fetch plot summaries from external sources for cross-checking."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import requests

LOGGER = logging.getLogger("videocatalog.textverify.plotsrc")


@dataclass(slots=True)
class PlotResult:
    text: str
    source: str
    excerpt: str


class PlotFetcher:
    """Fetch TMDb/IMDb/OpenSubtitles synopses for movies and episodes."""

    def __init__(
        self,
        *,
        tmdb_api_key: Optional[str],
        imdb_enabled: bool,
        opensubs_api_key: Optional[str],
        opensubs_timeout: float = 15.0,
    ) -> None:
        self.tmdb_api_key = tmdb_api_key or os.environ.get("TMDB_API_KEY")
        self.imdb_enabled = imdb_enabled
        self.opensubs_api_key = opensubs_api_key or os.environ.get("OPENSUBTITLES_API_KEY")
        self.opensubs_timeout = max(5.0, float(opensubs_timeout or 15.0))
        self._imdb_client = None

    def _ensure_imdb(self):
        if self._imdb_client is not None:
            return self._imdb_client
        if not self.imdb_enabled:
            return None
        try:
            from imdb import Cinemagoer  # type: ignore

            self._imdb_client = Cinemagoer()
            return self._imdb_client
        except Exception as exc:  # pragma: no cover - optional dependency
            LOGGER.info("IMDb client unavailable: %s", exc)
            self._imdb_client = None
            return None

    def _tmdb_get(self, path: str, params: Optional[Dict[str, object]] = None) -> Optional[Dict[str, object]]:
        if not self.tmdb_api_key:
            return None
        params = dict(params or {})
        params.setdefault("api_key", self.tmdb_api_key)
        params.setdefault("language", "en-US")
        url = f"https://api.themoviedb.org/3{path}"
        try:
            response = requests.get(url, params=params, timeout=15.0)
        except requests.RequestException as exc:  # pragma: no cover - network dependent
            LOGGER.debug("TMDb request failed: %s", exc)
            return None
        if response.status_code >= 300:
            LOGGER.debug("TMDb HTTP %s for %s", response.status_code, path)
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def movie_plot(
        self,
        movie_id: Optional[str],
        title: Optional[str],
        year: Optional[int],
        imdb_id: Optional[str] = None,
    ) -> Optional[PlotResult]:
        if imdb_id:
            imdb_plot = self._episode_from_imdb(imdb_id)
            if imdb_plot:
                imdb_plot.source = "imdb"
                return imdb_plot
        result = self._movie_from_tmdb(movie_id, title, year)
        if result:
            return result
        result = self._movie_from_imdb(title, year)
        if result:
            return result
        return None

    def episode_plot(
        self,
        *,
        tmdb_series_id: Optional[str],
        season_number: Optional[int],
        episode_numbers: Sequence[int],
        imdb_episode_id: Optional[str],
    ) -> Optional[PlotResult]:
        episode_no = None
        if episode_numbers:
            episode_no = episode_numbers[0]
        if tmdb_series_id and season_number is not None and episode_no is not None:
            payload = self._tmdb_get(
                f"/tv/{tmdb_series_id}/season/{season_number}/episode/{episode_no}",
                {},
            )
            if payload:
                overview = payload.get("overview")
                if isinstance(overview, str) and overview.strip():
                    excerpt = overview.strip()[:600]
                    return PlotResult(text=overview.strip(), source="tmdb", excerpt=excerpt)
        if imdb_episode_id:
            imdb_plot = self._episode_from_imdb(imdb_episode_id)
            if imdb_plot:
                return imdb_plot
        return None

    def _movie_from_tmdb(
        self,
        movie_id: Optional[str],
        title: Optional[str],
        year: Optional[int],
    ) -> Optional[PlotResult]:
        if movie_id:
            payload = self._tmdb_get(f"/movie/{movie_id}")
            if payload:
                overview = payload.get("overview")
                if isinstance(overview, str) and overview.strip():
                    excerpt = overview.strip()[:600]
                    return PlotResult(text=overview.strip(), source="tmdb", excerpt=excerpt)
        if not title:
            return None
        search_payload = self._tmdb_get("/search/movie", {"query": title, "year": year})
        if not search_payload:
            return None
        results = search_payload.get("results")
        if isinstance(results, list):
            for entry in results[:3]:
                if not isinstance(entry, dict):
                    continue
                overview = entry.get("overview")
                if isinstance(overview, str) and overview.strip():
                    excerpt = overview.strip()[:600]
                    return PlotResult(text=overview.strip(), source="tmdb", excerpt=excerpt)
        return None

    def _movie_from_imdb(self, title: Optional[str], year: Optional[int]) -> Optional[PlotResult]:
        client = self._ensure_imdb()
        if client is None or not title:
            return None
        try:
            results = client.search_movie(title)
        except Exception:  # pragma: no cover - network dependent
            return None
        for entry in results[:5]:
            if not hasattr(entry, "movieID"):
                continue
            if year is not None:
                try:
                    candidate_year = int(entry.get("year"))
                except Exception:
                    candidate_year = None
                if candidate_year and abs(candidate_year - year) > 2:
                    continue
            imdb_id = f"tt{entry.movieID}"
            plot = self._episode_from_imdb(imdb_id)
            if plot:
                plot.source = "imdb"
                return plot
        return None

    def _episode_from_imdb(self, imdb_id: str) -> Optional[PlotResult]:
        client = self._ensure_imdb()
        if client is None:
            return None
        try:
            movie = client.get_movie(imdb_id.replace("tt", ""))
        except Exception:  # pragma: no cover - network dependent
            return None
        plot_outline = movie.get("plot outline") if isinstance(movie, dict) else None
        if isinstance(plot_outline, str) and plot_outline.strip():
            excerpt = plot_outline.strip()[:600]
            return PlotResult(text=plot_outline.strip(), source="imdb", excerpt=excerpt)
        plots = movie.get("plot") if isinstance(movie, dict) else None
        if isinstance(plots, list):
            for item in plots:
                if not isinstance(item, str):
                    continue
                cleaned = item.split("::", 1)[0].strip()
                if cleaned:
                    return PlotResult(text=cleaned, source="imdb", excerpt=cleaned[:600])
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["PlotFetcher", "PlotResult", "iso_now"]
