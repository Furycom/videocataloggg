"""Confidence scoring for TV series hierarchy."""
from __future__ import annotations

from typing import Dict, Iterable, Sequence

from .tv_types import (
    TVConfidenceBreakdown,
    TVEpisodeVerification,
    TVScoringSettings,
    TVSeasonVerification,
    TVSeriesInfo,
    TVSeriesVerification,
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _apply_penalties(base: float, issues: Sequence[str]) -> float:
    if not issues:
        return base
    penalty = 0.05 * len(issues)
    return max(0.0, base - penalty)


def score_series(
    series: TVSeriesInfo,
    verification: TVSeriesVerification,
    *,
    has_nfo: bool,
    season_alignment: bool,
    issues: Sequence[str],
    settings: TVScoringSettings,
) -> TVConfidenceBreakdown:
    confidence = 0.0
    signals: Dict[str, float] = {}
    reasons = []
    if series.canonical:
        confidence += settings.series_canon
        signals["canonical"] = settings.series_canon
        reasons.append("series_canonical")
    if has_nfo:
        confidence += settings.series_nfo
        signals["nfo"] = settings.series_nfo
        reasons.append("series_nfo")
    if verification.tmdb_id and verification.name_score >= 0.6:
        confidence += settings.series_tmdb * min(1.0, verification.name_score + 0.2)
        signals["tmdb"] = settings.series_tmdb * min(1.0, verification.name_score + 0.2)
        reasons.append("series_tmdb_match")
    if season_alignment:
        confidence += settings.series_structure
        signals["structure"] = settings.series_structure
        reasons.append("season_alignment")
    confidence = _apply_penalties(confidence, issues)
    return TVConfidenceBreakdown(confidence=_clamp(confidence), reasons=reasons, signals=signals)


def score_season(
    season_verification: TVSeasonVerification,
    *,
    canonical: bool,
    has_nfo: bool,
    issues: Sequence[str],
    settings: TVScoringSettings,
) -> TVConfidenceBreakdown:
    confidence = 0.0
    signals: Dict[str, float] = {}
    reasons = []
    if canonical:
        confidence += settings.season_canon
        signals["canonical"] = settings.season_canon
        reasons.append("season_canonical")
    if season_verification.tmdb_season_id and season_verification.expected_episodes is not None:
        confidence += settings.season_tmdb
        signals["tmdb"] = settings.season_tmdb
        reasons.append("season_tmdb_match")
    if has_nfo:
        confidence += settings.season_nfo
        signals["nfo"] = settings.season_nfo
        reasons.append("season_nfo")
    confidence = _apply_penalties(confidence, issues)
    return TVConfidenceBreakdown(confidence=_clamp(confidence), reasons=reasons, signals=signals)


def score_episode(
    verification: TVEpisodeVerification,
    *,
    parsed_ok: bool,
    subtitles_present: bool,
    issues: Sequence[str],
    settings: TVScoringSettings,
) -> TVConfidenceBreakdown:
    confidence = 0.0
    signals: Dict[str, float] = {}
    reasons = []
    if parsed_ok:
        confidence += settings.episode_parse
        signals["parse"] = settings.episode_parse
        reasons.append("episode_parsed")
    if verification.tmdb_episode_id or verification.tmdb_air_date:
        confidence += settings.episode_tmdb
        signals["tmdb"] = settings.episode_tmdb
        reasons.append("episode_tmdb")
    if verification.oshash_match:
        confidence += settings.episode_oshash
        signals["oshash"] = settings.episode_oshash
        reasons.append("episode_oshash")
    if subtitles_present:
        confidence += settings.episode_langs
        signals["subtitles"] = settings.episode_langs
        reasons.append("subtitles_present")
    confidence = _apply_penalties(confidence, issues)
    return TVConfidenceBreakdown(confidence=_clamp(confidence), reasons=reasons, signals=signals)


def classify_confidence(confidence: float, settings: TVScoringSettings) -> str:
    if confidence >= settings.threshold_high:
        return "high"
    if confidence >= settings.threshold_low:
        return "medium"
    return "low"


__all__ = [
    "score_series",
    "score_season",
    "score_episode",
    "classify_confidence",
]
