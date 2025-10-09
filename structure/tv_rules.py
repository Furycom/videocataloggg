"""Canonical folder detection rules for TV series."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .rules import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS
from .tv_types import TVEpisodeFile, TVSeasonInfo, TVSeriesInfo

SERIES_CANONICAL_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)$")
SEASON_NUMBER_RE = re.compile(r"^(?:season\s*)?(?P<number>\d{1,2})$", re.IGNORECASE)
SEASON_CANONICAL_RE = re.compile(r"^(?:season\s*)?(?P<number>\d{1,2})$", re.IGNORECASE)
SEASON_SPECIALS_RE = re.compile(r"^(?:season\s*)?(?:specials|0{1,2})$", re.IGNORECASE)
SEASON_SHORT_RE = re.compile(r"^s(?P<number>\d{1,2})$", re.IGNORECASE)
EPISODE_TOKEN_RE = re.compile(r"S(?P<season>\d{1,2})E(?P<episode>\d{2})(?:E(?P<episode2>\d{2}))?", re.IGNORECASE)
DATE_TOKEN_RE = re.compile(r"(?P<year>20\d{2}|19\d{2})[-_.](?P<month>\d{2})[-_.](?P<day>\d{2})")
ABSOLUTE_TOKEN_RE = re.compile(r"(?:^|[^\d])(?P<number>\d{2,4})(?:[^\d]|$)")

LOCAL_SHOW_NFO = {"tvshow.nfo", "show.nfo"}
LOCAL_SEASON_NFO = {"season.nfo"}
LOCAL_EPISODE_NFO = {"episode.nfo"}
LOCAL_POSTERS = {"poster.jpg", "poster.png", "folder.jpg", "folder.png"}
LOCAL_FANART = {"fanart.jpg", "fanart.png", "background.jpg"}
EXTRAS_FOLDERS = {"extras", "featurettes", "bonus"}


def _iter_visible_entries(folder: Path) -> Iterator[os.DirEntry[str]]:
    with os.scandir(folder) as handle:
        for entry in handle:
            if entry.name.startswith("."):
                continue
            yield entry


def _collect_assets(entries: Iterable[Path]) -> Dict[str, object]:
    assets: Dict[str, object] = {
        "poster": False,
        "fanart": False,
        "subtitles": [],
        "nfo": None,
    }
    subtitles: List[str] = []
    extras: Dict[str, List[str]] = defaultdict(list)
    for entry in entries:
        lower = entry.name.lower()
        if lower in LOCAL_POSTERS:
            assets["poster"] = True
            continue
        if lower in LOCAL_FANART:
            assets["fanart"] = True
            continue
        if lower in LOCAL_SHOW_NFO or lower in LOCAL_SEASON_NFO or lower in LOCAL_EPISODE_NFO:
            assets["nfo"] = entry.name
            continue
        if entry.suffix.lower() in SUBTITLE_EXTENSIONS:
            subtitles.append(entry.name)
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        if is_dir and entry.name.lower() in EXTRAS_FOLDERS:
            extras[entry.name].append(entry.name)
    if subtitles:
        assets["subtitles"] = sorted(set(subtitles))
    if extras:
        assets["extras"] = {key: sorted(values) for key, values in extras.items()}
    return assets


def _detect_series_name(path: Path) -> Tuple[Optional[str], Optional[int], bool]:
    match = SERIES_CANONICAL_RE.match(path.name)
    if match:
        title = match.group("title").strip()
        year = int(match.group("year"))
        return title, year, True
    return path.name, None, False


def _detect_season_number(name: str) -> Tuple[Optional[int], bool, bool]:
    lowered = name.lower()
    clean = lowered.replace("_", " ").replace("-", " ")
    tokens = clean.split()
    canonical = False
    specials = False
    if len(tokens) == 2 and tokens[0] == "season":
        token = tokens[1]
        if token.isdigit():
            canonical = True
            return int(token), canonical, specials
        if token in {"0", "00"}:
            canonical = True
            specials = True
            return 0, canonical, specials
        if token == "specials":
            canonical = True
            specials = True
            return 0, canonical, specials
    if SEASON_SPECIALS_RE.match(clean):
        specials = True
        canonical = True
        return 0, canonical, specials
    match = SEASON_CANONICAL_RE.match(clean)
    if match and match.group("number"):
        canonical = True
        return int(match.group("number")), canonical, specials
    match = SEASON_SHORT_RE.match(clean)
    if match and match.group("number"):
        return int(match.group("number")), canonical, specials
    return None, canonical, specials


def _detect_episode_assets(folder: Path) -> Dict[str, object]:
    assets: Dict[str, object] = {"subtitles": []}
    subtitles: List[str] = []
    for entry in folder.iterdir():
        if entry.suffix.lower() in SUBTITLE_EXTENSIONS:
            subtitles.append(entry.name)
    if subtitles:
        assets["subtitles"] = sorted(subtitles)
    else:
        assets.pop("subtitles", None)
    return assets


def _collect_episode_files(folder: Path) -> List[TVEpisodeFile]:
    episodes: List[TVEpisodeFile] = []
    subtitle_map: Dict[str, List[Path]] = defaultdict(list)
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        suffix = entry.suffix.lower()
        if suffix in SUBTITLE_EXTENSIONS:
            subtitle_map[entry.stem].append(entry)
            continue
        if suffix not in VIDEO_EXTENSIONS:
            continue
        try:
            size_bytes = entry.stat().st_size
        except OSError:
            size_bytes = 0
        subtitles = subtitle_map.get(entry.stem, [])
        episodes.append(TVEpisodeFile(path=entry, size_bytes=size_bytes, subtitles=list(subtitles)))
    return sorted(episodes, key=lambda item: item.path.name)


def analyze_season_folder(path: Path) -> TVSeasonInfo:
    season_number, canonical, specials = _detect_season_number(path.name)
    issues: List[str] = []
    if specials and season_number != 0:
        issues.append("specials_misnumbered")
    entries = [Path(entry.path) for entry in _iter_visible_entries(path)]
    assets = _collect_assets(entries)
    episodes = _collect_episode_files(path)
    if not episodes:
        issues.append("no_episodes")
    return TVSeasonInfo(path=path, season_number=season_number, canonical=canonical, assets=assets, issues=issues, episodes=episodes)


def analyze_series_root(path: Path) -> TVSeriesInfo:
    title, year, canonical = _detect_series_name(path)
    entries = [Path(entry.path) for entry in _iter_visible_entries(path)]
    assets = _collect_assets(entries)
    seasons: List[TVSeasonInfo] = []
    issues: List[str] = []
    for entry in _iter_visible_entries(path):
        if entry.is_dir():
            season_info = analyze_season_folder(Path(entry.path))
            seasons.append(season_info)
    if not seasons:
        issues.append("no_seasons")
    return TVSeriesInfo(root=path, title=title, year=year, canonical=canonical, assets=assets, issues=issues, seasons=sorted(seasons, key=lambda s: (s.season_number if s.season_number is not None else 9999, s.path.name)))


__all__ = [
    "SERIES_CANONICAL_RE",
    "SEASON_CANONICAL_RE",
    "SEASON_SPECIALS_RE",
    "EPISODE_TOKEN_RE",
    "DATE_TOKEN_RE",
    "ABSOLUTE_TOKEN_RE",
    "analyze_series_root",
    "analyze_season_folder",
]
