"""Confidence scoring utilities for parsed music names."""

from __future__ import annotations

from typing import Iterable, Sequence

from .normalize import normalise_candidate
from .parse import ParseResult

_BASE_SCORE = 0.4
_DASH_BONUS = 0.1
_ARTIST_TITLE_BONUS = 0.3
_TRACK_BONUS = 0.05
_ALBUM_BONUS = 0.05
_PARENT_MATCH_BONUS = 0.05
_MISSING_FIELD_PENALTY = 0.2
_SUSPICIOUS_ARTIST_PENALTY = 0.25
_SUSPICIOUS_TITLE_PENALTY = 0.15

_SUSPICIOUS_ARTIST_TOKENS = {"unknown", "various", "untitled"}
_SUSPICIOUS_TITLE_TOKENS = {"track", "untitled", "unknown"}


def _contains_token(text: str, tokens: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in tokens)


def score_parse_result(result: ParseResult, parents: Sequence[str] | None = None) -> tuple[float, list[str]]:
    """Return (score, reasons) representing confidence in *result*."""

    score = _BASE_SCORE
    reasons: list[str] = ["base score"]

    if result.artist and result.title:
        score += _ARTIST_TITLE_BONUS
        reasons.append("artist and title")

    if result.track:
        score += _TRACK_BONUS
        reasons.append("track number")

    if result.album:
        score += _ALBUM_BONUS
        reasons.append("album context")

    if "dash split" in result.reasons:
        score += _DASH_BONUS
        reasons.append("dash split")

    if "parent artist match" in result.reasons:
        score += _PARENT_MATCH_BONUS
        reasons.append("parent artist match")

    if not result.artist:
        score -= _MISSING_FIELD_PENALTY
        reasons.append("missing artist")
    elif _contains_token(result.artist, _SUSPICIOUS_ARTIST_TOKENS):
        score -= _SUSPICIOUS_ARTIST_PENALTY
        reasons.append("suspicious artist")

    if not result.title:
        score -= _MISSING_FIELD_PENALTY
        reasons.append("missing title")
    elif _contains_token(result.title, _SUSPICIOUS_TITLE_TOKENS):
        score -= _SUSPICIOUS_TITLE_PENALTY
        reasons.append("suspicious title")

    if parents:
        normalised = [normalise_candidate(parent) for parent in parents]
        if result.artist and any(result.artist.lower() == parent.lower() for parent in normalised):
            if "parent artist match" not in reasons:
                reasons.append("parent artist match")
                score += _PARENT_MATCH_BONUS

    score = max(0.0, min(1.0, score))
    return score, reasons
