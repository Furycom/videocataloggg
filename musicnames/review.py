"""Utilities that turn low-confidence parses into human review hints."""

from __future__ import annotations

from typing import Iterable, Sequence

from .parse import ParseResult

_DEFAULT_THRESHOLD = 0.65
_SUFFIX_TOKENS = ("final", "copy", "master", "edit")


def _alternate_split_suggestion(raw_name: str, parsed_reasons: Iterable[str]) -> str | None:
    dash_count = sum(raw_name.count(dash) for dash in "-–—")
    if dash_count > 1 and "dash split" not in parsed_reasons:
        return "try alternate dash split"
    return None


def _parent_artist_suggestion(result: ParseResult) -> str | None:
    parents = result.raw.get("normalised_parents", [])
    if not isinstance(parents, Sequence) or not parents:
        return None
    target_artist = (result.artist or "").lower()
    album = (result.album or "").lower()
    for candidate in reversed(list(parents)):
        if not isinstance(candidate, str) or not candidate:
            continue
        lowered = candidate.lower()
        if lowered == target_artist or lowered == album:
            continue
        return f"use parent folder artist '{candidate}'"
    return None


def _ignore_suffix_suggestion(raw_name: str) -> str | None:
    lowered = raw_name.lower()
    for token in _SUFFIX_TOKENS:
        if lowered.endswith(token):
            return "ignore trailing suffix"
    return None


def _mark_unknown_suggestion(result: ParseResult) -> str | None:
    if not result.artist or not result.title:
        return "mark as unknown"
    return None


def generate_review_bundle(
    result: ParseResult,
    score: float,
    score_reasons: Sequence[str] | None = None,
    threshold: float = _DEFAULT_THRESHOLD,
) -> dict[str, object]:
    """Return review metadata for *result* when *score* falls below *threshold*."""

    needs_review = score < threshold
    reasons: list[str] = []
    suggestions: list[str] = []

    if not needs_review:
        return {
            "needs_review": False,
            "reasons": reasons,
            "suggestions": suggestions,
        }

    reasons.append(f"confidence {score:.2f} below {threshold:.2f}")

    if score_reasons:
        reasons.extend(score_reasons)

    base_name = str(result.raw.get("base_name", ""))
    alternate = _alternate_split_suggestion(base_name, result.reasons)
    if alternate:
        suggestions.append(alternate)

    parent_artist = _parent_artist_suggestion(result)
    if parent_artist:
        suggestions.append(parent_artist)

    suffix = _ignore_suffix_suggestion(base_name)
    if suffix:
        suggestions.append(suffix)

    unknown = _mark_unknown_suggestion(result)
    if unknown:
        suggestions.append(unknown)

    # Remove duplicate suggestions preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for suggestion in suggestions:
        if suggestion not in seen:
            seen.add(suggestion)
            deduped.append(suggestion)

    return {
        "needs_review": True,
        "reasons": reasons,
        "suggestions": deduped,
    }
