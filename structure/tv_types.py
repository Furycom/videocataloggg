"""Dataclasses shared by TV structure profiling modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence


@dataclass(slots=True)
class TVEpisodeFile:
    """Representation of a TV episode file detected on disk."""

    path: Path
    size_bytes: int
    subtitles: List[Path] = field(default_factory=list)
    audio_languages: List[str] = field(default_factory=list)
    parsed_title: Optional[str] = None


@dataclass(slots=True)
class TVEpisodeGuess:
    """Normalized GuessIt payload for an episode."""

    series: Optional[str] = None
    season: Optional[int] = None
    episodes: List[int] = field(default_factory=list)
    absolute_numbers: List[int] = field(default_factory=list)
    air_date: Optional[date] = None
    title: Optional[str] = None
    raw: Dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class TVSeasonInfo:
    """Filesystem-level information about a season folder."""

    path: Path
    season_number: Optional[int]
    canonical: bool
    assets: Dict[str, object] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    episodes: List[TVEpisodeFile] = field(default_factory=list)


@dataclass(slots=True)
class TVSeriesInfo:
    """Filesystem-level information about a series root."""

    root: Path
    title: Optional[str]
    year: Optional[int]
    canonical: bool
    assets: Dict[str, object] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    seasons: List[TVSeasonInfo] = field(default_factory=list)


@dataclass(slots=True)
class TVVerificationCandidate:
    """Candidate identification pulled from an external service."""

    source: str
    candidate_id: Optional[str]
    title: Optional[str]
    year: Optional[int]
    score: float
    extra: Dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class TVEpisodeVerification:
    """Verification metadata for a specific episode."""

    candidates: List[TVVerificationCandidate] = field(default_factory=list)
    tmdb_episode_id: Optional[str] = None
    imdb_episode_id: Optional[str] = None
    tmdb_air_date: Optional[str] = None
    oshash_match: Optional[Dict[str, object]] = None


@dataclass(slots=True)
class TVSeasonVerification:
    """Verification metadata for a season."""

    tmdb_season_id: Optional[str] = None
    expected_episodes: Optional[int] = None
    candidates: List[TVVerificationCandidate] = field(default_factory=list)


@dataclass(slots=True)
class TVSeriesVerification:
    """Verification metadata for a series root."""

    candidates: List[TVVerificationCandidate] = field(default_factory=list)
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    name_score: float = 0.0

    def top_candidates(self, limit: int) -> Sequence[TVVerificationCandidate]:
        return self.candidates[:limit]


@dataclass(slots=True)
class TVConfidenceBreakdown:
    """Confidence score and supporting reasons for an entity."""

    confidence: float
    reasons: List[str] = field(default_factory=list)
    signals: Dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class TVScoringSettings:
    """Weights and thresholds used for TV scoring."""

    series_canon: float = 0.30
    series_nfo: float = 0.25
    series_tmdb: float = 0.20
    series_structure: float = 0.10
    season_canon: float = 0.25
    season_tmdb: float = 0.25
    season_nfo: float = 0.15
    episode_parse: float = 0.30
    episode_tmdb: float = 0.25
    episode_oshash: float = 0.20
    episode_langs: float = 0.10
    threshold_low: float = 0.50
    threshold_high: float = 0.80


@dataclass(slots=True)
class TVSettings:
    """High level configuration for TV profiling."""

    enable: bool = True
    use_opensubtitles: bool = True
    opensubtitles_api_key: Optional[str] = None
    opensubtitles_read_kib: int = 64
    opensubtitles_timeout: float = 15.0
    tmdb_api_key: Optional[str] = None
    tmdb_lang: str = "en"
    tmdb_year_tolerance: int = 1
    tmdb_timeout: float = 15.0
    imdb_enabled: bool = True
    max_candidates: int = 5
    scoring: TVScoringSettings = field(default_factory=TVScoringSettings)


def load_tv_settings(data: Dict[str, object]) -> TVSettings:
    section = data.get("tv") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        section = {}
    scoring_section = section.get("weights") if isinstance(section.get("weights"), dict) else {}
    settings = TVSettings()
    settings.enable = bool(section.get("enable", settings.enable))
    settings.use_opensubtitles = bool(section.get("use_opensubtitles", settings.use_opensubtitles))
    settings.opensubtitles_api_key = (
        str(section.get("opensubtitles_api_key")).strip() or None
        if "opensubtitles_api_key" in section
        else settings.opensubtitles_api_key
    )
    try:
        settings.opensubtitles_read_kib = int(section.get("opensubtitles_read_kib", settings.opensubtitles_read_kib))
    except (TypeError, ValueError):
        pass
    try:
        settings.opensubtitles_timeout = float(section.get("opensubtitles_timeout", settings.opensubtitles_timeout))
    except (TypeError, ValueError):
        pass
    tmdb_section_raw = section.get("tmdb")
    if isinstance(tmdb_section_raw, str):
        settings.tmdb_api_key = tmdb_section_raw.strip() or settings.tmdb_api_key
        tmdb_section = {}
    elif isinstance(tmdb_section_raw, dict):
        tmdb_section = tmdb_section_raw
    else:
        tmdb_section = {}
    if tmdb_section:
        settings.tmdb_api_key = (
            str(tmdb_section.get("api_key")).strip() or None
            if "api_key" in tmdb_section
            else settings.tmdb_api_key
        )
        settings.tmdb_lang = str(tmdb_section.get("lang", settings.tmdb_lang))
        try:
            settings.tmdb_year_tolerance = int(tmdb_section.get("year_tolerance", settings.tmdb_year_tolerance))
        except (TypeError, ValueError):
            pass
    try:
        settings.tmdb_timeout = float(section.get("tmdb_timeout", settings.tmdb_timeout))
    except (TypeError, ValueError):
        pass
    settings.imdb_enabled = bool(section.get("imdb_enabled", settings.imdb_enabled))
    try:
        settings.max_candidates = max(1, int(section.get("max_candidates", settings.max_candidates)))
    except (TypeError, ValueError):
        pass
    scoring = settings.scoring
    for key, default in (
        ("series_canon", scoring.series_canon),
        ("series_nfo", scoring.series_nfo),
        ("series_tmdb", scoring.series_tmdb),
        ("series_structure", scoring.series_structure),
        ("season_canon", scoring.season_canon),
        ("season_tmdb", scoring.season_tmdb),
        ("season_nfo", scoring.season_nfo),
        ("ep_parse", scoring.episode_parse),
        ("ep_tmdb", scoring.episode_tmdb),
        ("ep_oshash", scoring.episode_oshash),
        ("ep_langs", scoring.episode_langs),
    ):
        try:
            setattr(scoring, key if not key.startswith("ep_") else key.replace("ep_", "episode_"), float(scoring_section.get(key, default)))
        except (TypeError, ValueError):
            continue
    try:
        scoring.threshold_low = float(section.get("threshold_low", scoring.threshold_low))
    except (TypeError, ValueError):
        pass
    try:
        scoring.threshold_high = float(section.get("threshold_high", scoring.threshold_high))
    except (TypeError, ValueError):
        pass
    return settings


__all__ = [
    "TVEpisodeFile",
    "TVEpisodeGuess",
    "TVSeasonInfo",
    "TVSeriesInfo",
    "TVVerificationCandidate",
    "TVEpisodeVerification",
    "TVSeasonVerification",
    "TVSeriesVerification",
    "TVConfidenceBreakdown",
    "TVScoringSettings",
    "TVSettings",
    "load_tv_settings",
]
