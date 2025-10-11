"""Utilities for discovering and sampling local subtitle files."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("videocatalog.textverify.subs")

_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
_TIMESTAMP_RE = re.compile(r"^\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d+\s*-->\s*\d+)")
_TAG_RE = re.compile(r"<[^>]+>")
_ASS_FIELD_SPLIT = re.compile(r"^Dialogue:\s*\d+,\s*[0-9:.]+,\s*[0-9:.]+,\s*([^,]*),")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class SubtitleSampleConfig:
    """Sampling limits for reading subtitle snippets."""

    max_lines: int = 400
    head_lines: int = 150
    mid_lines: int = 100
    tail_lines: int = 150
    max_chars: int = 20000


@dataclass(slots=True)
class SubtitleSnippet:
    """Small snippet extracted from a subtitle file."""

    text: str
    language: Optional[str]
    lines_used: int
    path: Optional[Path] = None


def _normalize_line(line: str, *, kind: str) -> str:
    text = line.strip()
    if not text:
        return ""
    if _TIMESTAMP_RE.match(text):
        return ""
    if text.isdigit():
        return ""
    if kind in {".ass", ".ssa"}:
        match = _ASS_FIELD_SPLIT.match(text)
        if match:
            text = text.split(",", 9)
            if len(text) >= 10:
                text = text[-1]
            else:
                text = match.group(1)
        elif text.lower().startswith("dialogue:"):
            parts = text.split(",", 9)
            text = parts[-1] if len(parts) >= 10 else parts[-1] if parts else ""
    text = _TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def discover_subtitles(video_path: Path) -> List[Path]:
    """Return subtitle files located alongside the given video."""

    if not video_path:
        return []
    directory = video_path.parent if video_path.is_file() else video_path
    if not directory.exists():
        return []
    base_stem = video_path.stem.lower()
    candidates: List[Tuple[int, Path]] = []
    try:
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext not in _SUBTITLE_EXTS:
                continue
            score = 0
            stem = entry.stem.lower()
            if stem == base_stem:
                score += 5
            if stem.startswith(base_stem):
                score += 3
            if ".en" in entry.name.lower():
                score += 2
            candidates.append((score, entry))
    except OSError as exc:
        LOGGER.debug("Unable to list subtitles near %s: %s", video_path, exc)
        return []
    candidates.sort(key=lambda item: (-item[0], item[1].name))
    return [path for _, path in candidates]


def _detect_language(text: str) -> Optional[str]:
    snippet = text.strip()[:4000]
    if not snippet:
        return None
    try:
        from langdetect import detect_langs  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        return None
    try:
        guesses = detect_langs(snippet)
    except Exception:  # pragma: no cover - runtime dependent
        return None
    if not guesses:
        return None
    top = guesses[0]
    prob = getattr(top, "prob", 0.0)
    lang = getattr(top, "lang", None)
    if prob and prob >= 0.55:
        return str(lang)
    return None


def _sample_lines(lines: Sequence[str], config: SubtitleSampleConfig) -> List[str]:
    if not lines:
        return []
    head = list(lines[: config.head_lines])
    if len(lines) <= config.head_lines:
        return head
    tail = list(lines[-config.tail_lines :]) if config.tail_lines > 0 else []
    middle = list(lines[config.head_lines : len(lines) - len(tail)])
    mid_target = max(0, config.mid_lines)
    middle_sample: List[str] = []
    if middle and mid_target > 0:
        step = max(1, len(middle) // mid_target)
        for index in range(0, len(middle), step):
            middle_sample.append(middle[index])
            if len(middle_sample) >= mid_target:
                break
    combined = head + middle_sample + tail
    if len(combined) > config.max_lines:
        combined = combined[: config.max_lines]
    return combined


def sample_subtitle_file(path: Path, config: SubtitleSampleConfig) -> SubtitleSnippet:
    """Read a limited snippet from the given subtitle file."""

    kind = path.suffix.lower()
    cleaned: List[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                text = _normalize_line(raw, kind=kind)
                if text:
                    cleaned.append(text)
                    if len(cleaned) >= max(config.max_lines * 4, config.max_lines + config.tail_lines + config.mid_lines):
                        break
    except OSError as exc:
        LOGGER.debug("Failed to read subtitles from %s: %s", path, exc)
        return SubtitleSnippet(text="", language=None, lines_used=0, path=path)
    sampled = _sample_lines(cleaned, config)
    if not sampled:
        return SubtitleSnippet(text="", language=None, lines_used=0, path=path)
    text = " ".join(sampled)
    if len(text) > config.max_chars:
        text = text[: config.max_chars]
    language = _detect_language(text)
    return SubtitleSnippet(text=text, language=language, lines_used=len(sampled), path=path)


def sample_from_video(video_path: Path, config: SubtitleSampleConfig) -> Optional[SubtitleSnippet]:
    """Discover subtitles near a video and return the first usable snippet."""

    for candidate in discover_subtitles(video_path):
        snippet = sample_subtitle_file(candidate, config)
        if snippet.text.strip():
            return snippet
    return None


__all__ = [
    "SubtitleSampleConfig",
    "SubtitleSnippet",
    "discover_subtitles",
    "sample_from_video",
    "sample_subtitle_file",
]
