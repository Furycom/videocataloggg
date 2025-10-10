"""Reusable normalisation helpers for music name parsing."""

from __future__ import annotations

import re
from typing import Iterable

from . import patterns

_AUDIO_EXTENSIONS = {
    "mp3",
    "flac",
    "aac",
    "m4a",
    "wav",
    "ogg",
    "wma",
    "alac",
    "aiff",
    "opus",
}

_SANITISED_SUFFIXES = (".part",)

_QUALITY_PATTERN = re.compile(
    r"\s*[\(\[\{](?:\d{3,4}p|\d{2,4}kbps|dvd|bluray)[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)


def strip_extension(name: str) -> str:
    """Remove a known audio extension from *name*."""

    if not name:
        return ""

    lowered = name.lower()
    for suffix in _SANITISED_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            name = name[: -len(suffix)]
            break

    base, dot, ext = name.rpartition(".")
    if dot and ext.lower() in _AUDIO_EXTENSIONS:
        return base
    return name


def swap_separators_for_spaces(text: str) -> str:
    """Replace underscores and dot runs with single spaces."""

    if not text:
        return ""
    swapped = re.sub(r"_+", " ", text)
    swapped = re.sub(r"(?<!\d)\.(?!\d)", " ", swapped)
    swapped = re.sub(r"\.\.+", " ", swapped)
    return re.sub(r"\s+", " ", swapped)


def collapse_spaces(text: str) -> str:
    """Normalise whitespace to single spaces and trim."""

    if not text:
        return ""
    return patterns.WHITESPACE_RE.sub(" ", text).strip()


def drop_bracketed_tags(text: str) -> str:
    """Remove bracketed metadata tokens recognised by :mod:`musicnames.patterns`."""

    if not text:
        return ""

    cleaned = patterns.BRACKET_TAG_RE.sub(" ", text)
    cleaned = _QUALITY_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"\s*[\(\[\{\}\]\)]\s*", " ", cleaned)
    return collapse_spaces(cleaned)


def normalise_candidate(text: str) -> str:
    """Convenience helper that applies all normalisation steps."""

    stripped = strip_extension(text)
    swapped = swap_separators_for_spaces(stripped)
    dropped = drop_bracketed_tags(swapped)
    return collapse_spaces(dropped)


def unique_non_empty(values: Iterable[str]) -> list[str]:
    """Return unique, case-insensitive values preserving order."""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        lowered = value.lower()
        if lowered not in seen:
            seen.add(lowered)
            result.append(value)
    return result
