"""Centralised regular expression patterns for music file parsing."""

from __future__ import annotations

import re

# Common dash characters used to separate artist and title tokens.
DASH_SPLIT_RE = re.compile(r"\s*(?:-|–|—|―|−)\s*")

# Words that usually indicate a featured artist.
FEATURED_TOKEN_RE = re.compile(
    r"\b(?:feat(?:\.|uring)?|ft\.|with|vs\.|x)\b",
    re.IGNORECASE,
)

# Separators that imply multiple artists are listed in the same field.
MULTI_ARTIST_SEPARATOR_RE = re.compile(
    r"\s*(?:,|&|\band\b|/|\\|\+|\s+x\s+|\s+vs\.?\s+)\s*",
    re.IGNORECASE,
)

# Leading track numbers (optionally prefixed by disc indicators) followed by punctuation.
TRACK_NUMBER_RE = re.compile(
    r"^(?P<track>(?:disc\s*)?[A-Z]?\d{1,3})(?:[\._\-\s]+)(?P<rest>.+)$",
    re.IGNORECASE,
)

# General bracket content detection.
BRACKET_CONTENT_RE = re.compile(r"[\(\[\{]([^\)\]\}]+)[\)\]\}]")

_BRACKET_KEYWORDS = (
    "hq hd remaster remix mix bonus live explicit clean single album lp ep ost soundtrack official "
    "lyrics audio visualizer radio edit original mix version instrumental acoustic demo cover extended "
    "reissue deluxe mono stereo quality release group tag cd disc"
).split()

# Bracketed metadata that should usually be stripped from titles.
BRACKET_TAG_RE = re.compile(
    (
        r"\s*[\(\[\{](?P<tag>[^\)\]\}]*?\b("
        + "|".join(_BRACKET_KEYWORDS)
        + r")\b[^\)\]\}]*)[\)\]\}]\s*"
    ),
    re.IGNORECASE,
)

# Collapsing whitespace.
WHITESPACE_RE = re.compile(r"\s+")
