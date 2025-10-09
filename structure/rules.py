"""Canonical folder rules and anomaly detection."""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .types import FolderAnalysis, VideoFile

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".m4v",
    ".ts",
    ".m2ts",
    ".webm",
    ".mpg",
    ".mpeg",
    ".vob",
    ".flv",
    ".3gp",
    ".ogv",
    ".mts",
    ".m2t",
}

SUBTITLE_EXTENSIONS = {
    ".srt",
    ".sub",
    ".idx",
    ".ssa",
    ".ass",
    ".vtt",
}

LOCAL_POSTERS = {"poster.jpg", "poster.png", "cover.jpg", "cover.png"}
LOCAL_FANART = {"fanart.jpg", "fanart.png", "backdrop.jpg", "background.jpg"}
LOCAL_NFO = {"movie.nfo", "index.nfo"}

_CANONICAL_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)$")
_SERIES_TOKEN_RE = re.compile(r"S\d{1,2}E\d{1,2}", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def _is_subtitle(path: Path) -> bool:
    return path.suffix.lower() in SUBTITLE_EXTENSIONS


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _collect_assets(files: Iterable[Path]) -> Tuple[Dict[str, object], Dict[str, List[str]]]:
    assets: Dict[str, object] = {
        "poster": False,
        "fanart": False,
        "subtitles": [],
        "nfo": None,
        "extras": [],
    }
    extras: Dict[str, List[str]] = defaultdict(list)
    for file_path in files:
        name = file_path.name.lower()
        if name in LOCAL_POSTERS:
            assets["poster"] = True
            continue
        if name in LOCAL_FANART:
            assets["fanart"] = True
            continue
        if name in LOCAL_NFO:
            assets["nfo"] = file_path.name
            continue
        if _is_subtitle(file_path):
            subtitles: List[str] = assets.setdefault("subtitles", [])  # type: ignore[assignment]
            subtitles.append(file_path.name)
            continue
        extras[file_path.suffix.lower()].append(file_path.name)
    if assets.get("subtitles"):
        assets["subtitles"] = sorted(set(assets["subtitles"]))  # type: ignore[index]
    if extras:
        assets["extras"] = {key.lstrip("."): sorted(values) for key, values in extras.items()}
    else:
        assets.pop("extras", None)
    return assets, extras


def _classify_kind(name: str, video_count: int, subtitle_count: int) -> str:
    lowered = name.lower()
    if _SERIES_TOKEN_RE.search(name) or "season" in lowered:
        return "series"
    if video_count == 0:
        return "other"
    if video_count >= 2:
        if _SERIES_TOKEN_RE.search("".join(lowered.split())):
            return "series"
        return "other"
    if "extras" in lowered or "featurette" in lowered:
        return "other"
    return "movie"


def profile_folder(folder_path: Path, *, rel_path: str) -> FolderAnalysis:
    """Inspect *folder_path* collecting rule-based observations."""

    entries: List[Path] = []
    try:
        for entry in os.scandir(folder_path):
            if _is_hidden(entry.name):
                continue
            try:
                path = Path(entry.path)
            except FileNotFoundError:
                continue
            if entry.is_dir(follow_symlinks=False):
                entries.append(path)
            elif entry.is_file(follow_symlinks=False):
                entries.append(path)
    except FileNotFoundError:
        analysis = FolderAnalysis(
            folder_path=folder_path,
            rel_path=rel_path,
            kind="unknown",
            canonical=False,
        )
        analysis.issues.append("unreadable_folder")
        return analysis

    files = [p for p in entries if p.is_file()]
    video_files: List[VideoFile] = []
    subtitle_count = 0
    issues: List[str] = []
    for file_path in files:
        if _is_video(file_path):
            try:
                size_bytes = file_path.stat().st_size
            except OSError:
                size_bytes = 0
            video_files.append(
                VideoFile(
                    path=file_path,
                    size_bytes=size_bytes,
                    basename=file_path.name,
                )
            )
        elif _is_subtitle(file_path):
            subtitle_count += 1

    assets, extras = _collect_assets(files)

    canonical_match = _CANONICAL_RE.match(folder_path.name)
    canonical = canonical_match is not None and len(video_files) == 1

    kind = _classify_kind(folder_path.name, len(video_files), subtitle_count)

    if len(video_files) == 0:
        issues.append("no_primary_video")
    elif len(video_files) > 1:
        issues.append("multiple_primary_videos")

    detected_year = None
    if canonical_match is None and kind == "movie":
        issues.append("missing_canonical_pattern")
    elif canonical_match is not None:
        try:
            year = int(canonical_match.group("year"))
        except Exception:
            issues.append("invalid_year")
            year = None
        else:
            if 1900 <= year <= 2100:
                detected_year = year
            else:
                issues.append("invalid_year")

    if kind == "movie" and detected_year is None:
        issues.append("missing_year")

    nfo_files = [p for p in files if p.suffix.lower() == ".nfo" or p.name.lower() in LOCAL_NFO]
    if not nfo_files and kind == "movie":
        issues.append("missing_nfo")

    if extras:
        for ext, names in extras.items():
            if ext in {".txt", ".jpg", ".png"}:
                continue
            issues.append(f"extra_{ext.lstrip('.')}_files:{len(names)}")

    return FolderAnalysis(
        folder_path=folder_path,
        rel_path=rel_path,
        kind=kind,
        canonical=canonical,
        video_files=video_files,
        assets=assets,
        issues=issues,
        nfo_files=nfo_files,
        detected_year=detected_year,
    )


def detect_year_from_files(names: Iterable[str]) -> int | None:
    counts = Counter()
    for name in names:
        for match in _YEAR_RE.finditer(name):
            counts[match.group(0)] += 1
    if not counts:
        return None
    year, _ = counts.most_common(1)[0]
    try:
        value = int(year)
    except ValueError:
        return None
    if 1900 <= value <= 2100:
        return value
    return None


__all__ = [
    "VIDEO_EXTENSIONS",
    "SUBTITLE_EXTENSIONS",
    "profile_folder",
    "detect_year_from_files",
]
