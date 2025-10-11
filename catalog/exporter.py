"""Catalog export utilities for VideoCatalog."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from api.db import DataAccess
from core.paths import get_exports_dir

def _timestamp_dir(base: Path) -> Path:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
    target = base / "catalog" / timestamp
    target.mkdir(parents=True, exist_ok=True)
    return target


def _iter_all_movies(data: DataAccess) -> List[Dict[str, object]]:
    movies: List[Dict[str, object]] = []
    offset = 0
    page_size = data.max_page_size
    while True:
        rows, pagination, next_offset, _ = data.catalog_movies_page(limit=page_size, offset=offset)
        movies.extend(rows)
        if next_offset is None:
            break
        offset = next_offset
    return movies


def _iter_all_series(data: DataAccess) -> List[Dict[str, object]]:
    series: List[Dict[str, object]] = []
    offset = 0
    page_size = data.max_page_size
    while True:
        rows, pagination, next_offset, _ = data.catalog_tv_series_page(limit=page_size, offset=offset)
        series.extend(rows)
        if next_offset is None:
            break
        offset = next_offset
    return series


def _iter_all_episodes(data: DataAccess) -> List[Dict[str, object]]:
    episodes: List[Dict[str, object]] = []
    for series in _iter_all_series(data):
        series_id = series["id"]
        series_episodes, _ = data.catalog_tv_episodes(series_id)
        episodes.extend(series_episodes)
    return episodes


def _movie_json(movie: Dict[str, object]) -> Dict[str, object]:
    return {
        "id": movie.get("id"),
        "title": movie.get("title"),
        "year": movie.get("year"),
        "drive": movie.get("drive"),
        "path": movie.get("path"),
        "confidence": movie.get("confidence"),
        "quality": movie.get("quality"),
        "langs_audio": movie.get("langs_audio") or [],
        "langs_subs": movie.get("langs_subs") or [],
    }


def _episode_json(episode: Dict[str, object]) -> Dict[str, object]:
    return {
        "id": episode.get("id"),
        "title": episode.get("title"),
        "drive": episode.get("drive"),
        "episode_path": episode.get("episode_path"),
        "season_number": episode.get("season_number"),
        "episode_numbers": episode.get("episode_numbers") or [],
        "confidence": episode.get("confidence"),
        "quality": episode.get("quality"),
        "air_date": episode.get("air_date"),
    }


def _write_jsonl(path: Path, rows: Iterable[Dict[str, object]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def _write_csv(path: Path, rows: Sequence[Dict[str, object]], headers: Sequence[str]) -> int:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(headers))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in headers})
    return len(rows)


def _nfo_movie_xml(detail: Dict[str, object]) -> bytes:
    root = ET.Element("movie")
    if detail.get("title"):
        ET.SubElement(root, "title").text = str(detail["title"])
    if detail.get("year"):
        ET.SubElement(root, "year").text = str(detail["year"])
    if detail.get("runtime_minutes"):
        ET.SubElement(root, "runtime").text = str(detail["runtime_minutes"])
    if detail.get("overview"):
        ET.SubElement(root, "plot").text = str(detail["overview"])
    for key, value in (detail.get("ids") or {}).items():
        if not value:
            continue
        tag = ET.SubElement(root, "uniqueid")
        tag.set("type", str(key))
        tag.text = str(value)
    return ET.tostring(root, encoding="utf-8")


def _nfo_episode_xml(detail: Dict[str, object]) -> bytes:
    root = ET.Element("episodedetails")
    if detail.get("title"):
        ET.SubElement(root, "title").text = str(detail["title"])
    if detail.get("season_number") is not None:
        ET.SubElement(root, "season").text = str(detail["season_number"])
    episodes = detail.get("episode_numbers") or []
    if episodes:
        ET.SubElement(root, "episode").text = ",".join(str(num) for num in episodes)
    if detail.get("air_date"):
        ET.SubElement(root, "aired").text = str(detail["air_date"])
    if detail.get("runtime_minutes"):
        ET.SubElement(root, "runtime").text = str(detail["runtime_minutes"])
    for key, value in (detail.get("ids") or {}).items():
        if not value:
            continue
        tag = ET.SubElement(root, "uniqueid")
        tag.set("type", str(key))
        tag.text = str(value)
    return ET.tostring(root, encoding="utf-8")


def _poster_bytes(token: Optional[str], data: DataAccess) -> Optional[bytes]:
    if not token:
        return None
    blob = data.catalog_fetch_media_blob(token)
    if blob is None:
        return None
    payload, _ = blob
    return payload


def export_catalog(
    data: DataAccess,
    *,
    format: str,
    scope: str,
) -> List[Path]:
    working_dir = data.working_dir
    base_exports = get_exports_dir(working_dir)
    target_dir = _timestamp_dir(base_exports)
    format = format.lower()
    scope = scope.lower()
    movies = _iter_all_movies(data) if scope in {"movies", "all"} else []
    episodes = _iter_all_episodes(data) if scope in {"tv", "all"} else []
    written: List[Path] = []
    if format == "jsonl":
        if movies:
            path = target_dir / "movies.jsonl"
            _write_jsonl(path, (_movie_json(row) for row in movies))
            written.append(path)
        if episodes:
            path = target_dir / "tv_episodes.jsonl"
            _write_jsonl(path, (_episode_json(row) for row in episodes))
            written.append(path)
        return written
    if format == "csv":
        if movies:
            path = target_dir / "movies.csv"
            headers = ["id", "title", "year", "drive", "path", "confidence", "quality"]
            rows = [_movie_json(row) for row in movies]
            _write_csv(path, rows, headers)
            written.append(path)
        if episodes:
            path = target_dir / "tv_episodes.csv"
            headers = ["id", "title", "drive", "episode_path", "season_number", "confidence", "quality"]
            rows = [_episode_json(row) for row in episodes]
            _write_csv(path, rows, headers)
            written.append(path)
        return written
    if format == "nfozip":
        archive_path = target_dir / "nfo_like.zip"
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            for movie in movies:
                detail = data.catalog_movie_detail(movie.get("drive"), movie.get("path"))
                if not detail:
                    continue
                xml_bytes = _nfo_movie_xml(detail)
                name = f"movies/{_safe_name(movie.get('title') or movie.get('path'))}.nfo"
                archive.writestr(name, xml_bytes)
                poster = _poster_bytes(detail.get("poster_thumb"), data)
                if poster:
                    archive.writestr(name.replace(".nfo", ".jpg"), poster)
            for episode in episodes:
                detail = data.catalog_episode_detail(episode.get("drive"), episode.get("episode_path"))
                if not detail:
                    continue
                xml_bytes = _nfo_episode_xml(detail)
                base_name = _safe_name(episode.get("title") or episode.get("episode_path"))
                name = f"episodes/{base_name}.nfo"
                archive.writestr(name, xml_bytes)
                poster = _poster_bytes(detail.get("poster_thumb"), data)
                if poster:
                    archive.writestr(name.replace(".nfo", ".jpg"), poster)
        written.append(archive_path)
        return written
    raise ValueError("unsupported export format")


def _safe_name(value: Optional[str]) -> str:
    if not value:
        return "item"
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value))
    return cleaned[:120] or "item"

