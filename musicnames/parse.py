"""Pure string parsing helpers that build candidate music metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, MutableMapping, Sequence

from . import normalize, patterns


@dataclass
class ParseResult:
    artist: str | None
    title: str | None
    album: str | None
    track: str | None
    reasons: list[str]
    raw: MutableMapping[str, object]


def _basename(path: str) -> str:
    candidate = path.rstrip("/\\")
    for separator in ("/", "\\"):
        if separator in candidate:
            candidate = candidate.rsplit(separator, 1)[-1]
    return candidate


def _normalise_parent_names(parents: Iterable[str]) -> list[str]:
    return [normalize.normalise_candidate(parent) for parent in parents]


def _extract_track_prefix(name: str, reasons: list[str]) -> tuple[str, str | None]:
    match = patterns.TRACK_NUMBER_RE.match(name)
    if not match:
        return name, None
    track = match.group("track").strip()
    remainder = match.group("rest").strip()
    reasons.append("track prefix")
    return remainder, track


def _strip_featured_artist(text: str, reasons: list[str]) -> str:
    if not text:
        return text
    if patterns.FEATURED_TOKEN_RE.search(text):
        primary = patterns.FEATURED_TOKEN_RE.split(text, maxsplit=1)[0].strip()
        if primary:
            reasons.append("featured artist token")
            return primary
    return text


def parse_music_name(path: str, parents: Sequence[str] | None = None) -> ParseResult:
    """Parse *path* using *parents* to form best-guess artist/title metadata."""

    parents = list(parents or [])
    raw: MutableMapping[str, object] = {
        "path": path,
        "parents": parents[:],
    }

    base_name = _basename(path)
    raw["base_name"] = base_name

    stripped = normalize.strip_extension(base_name)
    if stripped != base_name:
        raw["stripped_name"] = stripped

    swapped = normalize.swap_separators_for_spaces(stripped)
    if swapped != stripped:
        raw["swapped_name"] = swapped

    collapsed = normalize.collapse_spaces(swapped)
    if collapsed != swapped:
        raw["collapsed_name"] = collapsed

    cleaned = normalize.drop_bracketed_tags(collapsed)
    raw["cleaned_name"] = cleaned

    reasons: list[str] = []

    cleaned, track = _extract_track_prefix(cleaned, reasons)

    dash_parts = patterns.DASH_SPLIT_RE.split(cleaned, maxsplit=2)
    dash_parts = [part.strip() for part in dash_parts if part.strip()]

    artist: str | None = None
    title: str | None = None

    if len(dash_parts) >= 2:
        artist = dash_parts[0]
        title = " - ".join(dash_parts[1:])
        reasons.append("dash split")
    else:
        title = cleaned.strip() or None

    if artist:
        artist = normalize.collapse_spaces(artist)
        artist = _strip_featured_artist(artist, reasons)
        if patterns.MULTI_ARTIST_SEPARATOR_RE.search(artist):
            reasons.append("multi artist separator")
    if title:
        title = normalize.collapse_spaces(title)

    normalised_parents = _normalise_parent_names(parents)
    raw["normalised_parents"] = normalised_parents

    album: str | None = None
    parent_artist: str | None = None

    if normalised_parents:
        album_candidate = normalised_parents[-1]
        if album_candidate and album_candidate.lower() != (artist or "").lower():
            album = album_candidate
            reasons.append("album from parent")

    for candidate in reversed(normalised_parents):
        if candidate and candidate.lower() != (album or "").lower():
            parent_artist = candidate
            break

    if not artist and parent_artist:
        artist = parent_artist
        reasons.append("parent artist fallback")
    elif artist and parent_artist and artist.lower() == parent_artist.lower():
        reasons.append("parent artist match")

    return ParseResult(
        artist=artist or None,
        title=title or None,
        album=album,
        track=track,
        reasons=reasons,
        raw=raw,
    )
