"""CSV and JSON exporters for the VideoCatalog audit pack."""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from .summary import AuditSummary

LOGGER = logging.getLogger("videocatalog.audit.exports")

EXPORT_ROOT = "audit"


def _row_value(row: sqlite3.Row, key: str) -> object:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


@dataclass(slots=True)
class AuditExportResult:
    directory: Path
    files: List[Path] = field(default_factory=list)

    def add(self, path: Path) -> None:
        if path not in self.files:
            self.files.append(path)


def _timestamp_folder() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).strftime("%Y%m%d-%H%M%S")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _column_names(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _gentle_sleep(delay: float) -> None:
    if delay > 0:
        time.sleep(delay)


def _prepare_export_dir(working_dir: Path) -> Path:
    base = Path(working_dir, "exports", EXPORT_ROOT, _timestamp_folder())
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_csv(path: Path, headers: List[str], rows: Iterable[Dict[str, object]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    LOGGER.info("Wrote %s rows to %s", count, path)
    return count


def _write_json(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    LOGGER.info("Wrote JSON payload to %s", path)


def _parse_reasons(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return str(value)
    if isinstance(parsed, list):
        return "; ".join(str(item) for item in parsed)
    if isinstance(parsed, dict):
        return "; ".join(f"{key}={parsed[key]}" for key in parsed)
    return str(parsed)


def _format_candidates(rows: Iterable[sqlite3.Row]) -> str:
    formatted: List[str] = []
    for row in rows:
        source = str(_row_value(row, "source") or "?")
        candidate_id = str(_row_value(row, "candidate_id") or "").strip()
        label_parts: List[str] = []
        if source:
            label_parts.append(source)
        if candidate_id:
            label_parts.append(candidate_id)
        label = ":".join(label_parts) if label_parts else source
        title = _row_value(row, "title")
        year = _row_value(row, "year")
        if title:
            label = f"{label} {title}"
        if year not in (None, ""):
            label = f"{label} ({year})"
        score = _row_value(row, "score")
        try:
            score_val = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_val = None
        if score_val is not None:
            label = f"{label} [{score_val:.2f}]"
        formatted.append(label.strip())
    return "; ".join(item for item in formatted if item)


def _fetch_candidates(
    conn: sqlite3.Connection,
    keys: Iterable[str],
    *,
    gentle_sleep: float,
) -> Dict[str, List[sqlite3.Row]]:
    unique = sorted({key for key in keys if key})
    if not unique or not _table_exists(conn, "folder_candidates"):
        return {}
    result: Dict[str, List[sqlite3.Row]] = {}
    conn.row_factory = sqlite3.Row
    chunk = 200
    for start in range(0, len(unique), chunk):
        subset = unique[start : start + chunk]
        placeholders = ",".join("?" for _ in subset)
        cursor = conn.execute(
            f"""
            SELECT folder_path, source, candidate_id, title, year, score
            FROM folder_candidates
            WHERE folder_path IN ({placeholders})
            ORDER BY folder_path, score DESC
            """,
            subset,
        )
        rows = cursor.fetchall()
        for row in rows:
            folder_path = row["folder_path"]
            result.setdefault(folder_path, []).append(row)
        _gentle_sleep(gentle_sleep)
    conn.row_factory = None
    return result


def _iter_low_confidence_movies(
    conn: sqlite3.Connection, *, gentle_sleep: float
) -> Iterator[Dict[str, object]]:
    if not _table_exists(conn, "review_queue"):
        return iter(())
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT rq.folder_path, rq.confidence, rq.reasons_json,
               fp.main_video_path, fp.parsed_title, fp.parsed_year
        FROM review_queue AS rq
        LEFT JOIN folder_profile AS fp ON fp.folder_path = rq.folder_path
        ORDER BY rq.confidence ASC, rq.folder_path
        """
    )
    rows = []
    keys: List[str] = []
    for row in cursor.fetchall():
        rows.append(row)
        keys.append(row["folder_path"])
    candidates = _fetch_candidates(conn, keys, gentle_sleep=gentle_sleep)
    for row in rows:
        reasons = _parse_reasons(row["reasons_json"])
        folder_path = row["folder_path"]
        candidate_rows = candidates.get(folder_path, [])[:3]
        yield {
            "path": row["main_video_path"] or folder_path,
            "confidence": f"{float(row["confidence"] or 0.0):.2f}",
            "reasons": reasons,
            "parsed_title": row["parsed_title"] or "",
            "parsed_year": row["parsed_year"] or "",
            "candidate_ids": _format_candidates(candidate_rows),
        }
    conn.row_factory = None


def _iter_low_confidence_episodes(
    conn: sqlite3.Connection, *, gentle_sleep: float
) -> Iterator[Dict[str, object]]:
    if not _table_exists(conn, "tv_review_queue"):
        return iter(())
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT rq.item_key, rq.confidence, rq.reasons_json,
               ep.parsed_title, ep.ids_json, ep.episode_path, ep.season_number,
               ep.episode_numbers_json
        FROM tv_review_queue AS rq
        LEFT JOIN tv_episode_profile AS ep ON ep.episode_path = rq.item_key
        WHERE rq.item_type = 'episode'
        ORDER BY rq.confidence ASC, rq.item_key
        """
    )
    rows = cursor.fetchall()
    for row in rows:
        reasons = _parse_reasons(row["reasons_json"])
        ids_payload = row["ids_json"]
        candidates: List[str] = []
        if ids_payload:
            try:
                ids = json.loads(ids_payload)
            except json.JSONDecodeError:
                ids = {}
            if isinstance(ids, dict):
                for key in ("tmdb_series_id", "tmdb_id", "imdb_episode_id", "imdb_id"):
                    value = ids.get(key)
                    if value:
                        candidates.append(f"{key}:{value}")
        yield {
            "path": row["episode_path"] or row["item_key"],
            "confidence": f"{float(row["confidence"] or 0.0):.2f}",
            "reasons": reasons,
            "parsed_title": row["parsed_title"] or "",
            "parsed_year": "",
            "candidate_ids": "; ".join(candidates[:3]),
        }
    conn.row_factory = None


def _iter_probable_duplicates(
    conn: sqlite3.Connection,
) -> Iterator[Dict[str, object]]:
    if not _table_exists(conn, "duplicate_candidates"):
        return iter(())
    columns = _column_names(conn, "duplicate_candidates")
    has_duration = "duration_delta" in columns
    has_resolution = any(col in columns for col in ("resolution_delta", "res_delta"))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT * FROM duplicate_candidates ORDER BY score DESC"
    )
    rows = cursor.fetchall()
    for row in rows:
        payload = {
            "path_a": row["path_a"],
            "path_b": row["path_b"],
            "score": f"{float(row["score"] or 0.0):.4f}",
            "reason": row["reason"],
        }
        if has_duration:
            payload["duration_delta"] = _row_value(row, "duration_delta")
        if has_resolution:
            payload["res_delta"] = _row_value(row, "resolution_delta") or _row_value(
                row, "res_delta"
            )
        yield payload
    conn.row_factory = None


def _iter_unresolved_movies(
    conn: sqlite3.Connection, *, gentle_sleep: float
) -> Iterator[Dict[str, object]]:
    if not _table_exists(conn, "folder_profile"):
        return iter(())
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT folder_path, main_video_path, parsed_title, parsed_year,
               assets_json, source_signals_json
        FROM folder_profile
        ORDER BY folder_path
        """
    )
    rows = []
    keys: List[str] = []
    for row in cursor.fetchall():
        rows.append(row)
        keys.append(row["folder_path"])
    candidates = _fetch_candidates(conn, keys, gentle_sleep=gentle_sleep)
    for row in rows:
        assets = row["assets_json"]
        signals = row["source_signals_json"]
        if _has_confirmed_ids(assets) or _has_confirmed_ids(signals):
            continue
        folder_path = row["folder_path"]
        candidate_rows = candidates.get(folder_path, [])[:3]
        yield {
            "kind": "movie",
            "path": row["main_video_path"] or folder_path,
            "parsed_title": row["parsed_title"] or "",
            "parsed_year": row["parsed_year"] or "",
            "candidate_ids": _format_candidates(candidate_rows),
        }
    conn.row_factory = None


def _has_confirmed_ids(blob: Optional[str]) -> bool:
    if not blob:
        return False
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return False
    if isinstance(payload, dict):
        ids_obj = payload.get("ids") if isinstance(payload.get("ids"), dict) else payload
        if isinstance(ids_obj, dict):
            for key in ("tmdb", "tmdb_id", "imdb", "imdb_id", "tvdb", "tvdb_id"):
                value = ids_obj.get(key)
                if value:
                    return True
    return False


def _iter_unresolved_episodes(
    conn: sqlite3.Connection,
) -> Iterator[Dict[str, object]]:
    if not _table_exists(conn, "tv_episode_profile"):
        return iter(())
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT episode_path, parsed_title, ids_json, season_number, episode_numbers_json
        FROM tv_episode_profile
        ORDER BY episode_path
        """
    )
    rows = cursor.fetchall()
    for row in rows:
        ids_blob = row["ids_json"]
        if _has_confirmed_ids(ids_blob):
            continue
        candidates: List[str] = []
        if ids_blob:
            try:
                payload = json.loads(ids_blob)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                for key in ("tmdb_series_id", "tmdb_id", "imdb_episode_id", "imdb_id"):
                    value = payload.get(key)
                    if value:
                        candidates.append(f"{key}:{value}")
        yield {
            "kind": "episode",
            "path": row["episode_path"],
            "parsed_title": row["parsed_title"] or "",
            "parsed_year": "",
            "candidate_ids": "; ".join(candidates[:3]),
        }
    conn.row_factory = None


def _iter_quality_flags(conn: sqlite3.Connection) -> Iterator[Dict[str, object]]:
    if not _table_exists(conn, "video_quality"):
        return iter(())
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT path, score, reasons_json, width, height, video_codec,
               audio_channels_max, subs_present
        FROM video_quality
        ORDER BY updated_utc DESC
        """
    )
    rows = cursor.fetchall()
    for row in rows:
        reasons = _parse_reasons(row["reasons_json"])
        yield {
            "path": row["path"],
            "score": row["score"],
            "reasons": reasons,
            "width": row["width"],
            "height": row["height"],
            "vcodec": row["video_codec"],
            "audio_max_ch": row["audio_channels_max"],
            "subs_present": row["subs_present"],
        }
    conn.row_factory = None


def export_audit_data(
    conn: sqlite3.Connection,
    *,
    working_dir: Path,
    summary: AuditSummary,
    gentle_sleep: float = 0.02,
) -> AuditExportResult:
    """Export audit datasets to CSV/JSON files under the working directory."""

    export_dir = _prepare_export_dir(working_dir)
    LOGGER.info("Audit exports will be saved under %s", export_dir)
    result = AuditExportResult(directory=export_dir)

    movie_rows = list(_iter_low_confidence_movies(conn, gentle_sleep=gentle_sleep))
    if movie_rows:
        csv_path = export_dir / "low_confidence_movies.csv"
        _write_csv(
            csv_path,
            ["path", "confidence", "reasons", "parsed_title", "parsed_year", "candidate_ids"],
            movie_rows,
        )
        result.add(csv_path)
        json_path = export_dir / "low_confidence_movies.json"
        _write_json(json_path, movie_rows)
        result.add(json_path)

    episode_rows = list(
        _iter_low_confidence_episodes(conn, gentle_sleep=gentle_sleep)
    )
    if episode_rows:
        csv_path = export_dir / "low_confidence_episodes.csv"
        _write_csv(
            csv_path,
            ["path", "confidence", "reasons", "parsed_title", "parsed_year", "candidate_ids"],
            episode_rows,
        )
        result.add(csv_path)
        json_path = export_dir / "low_confidence_episodes.json"
        _write_json(json_path, episode_rows)
        result.add(json_path)

    duplicate_rows = list(_iter_probable_duplicates(conn))
    if duplicate_rows:
        csv_path = export_dir / "probable_duplicates.csv"
        headers = ["path_a", "path_b", "score", "reason"]
        if any("duration_delta" in row for row in duplicate_rows):
            headers.append("duration_delta")
        if any("res_delta" in row for row in duplicate_rows):
            headers.append("res_delta")
        _write_csv(csv_path, headers, duplicate_rows)
        result.add(csv_path)
        json_path = export_dir / "probable_duplicates.json"
        _write_json(json_path, duplicate_rows)
        result.add(json_path)

    unresolved_rows: List[Dict[str, object]] = []
    unresolved_rows.extend(_iter_unresolved_movies(conn, gentle_sleep=gentle_sleep))
    unresolved_rows.extend(_iter_unresolved_episodes(conn))
    if unresolved_rows:
        csv_path = export_dir / "unresolved_ids.csv"
        _write_csv(csv_path, ["kind", "path", "parsed_title", "parsed_year", "candidate_ids"], unresolved_rows)
        result.add(csv_path)
        json_path = export_dir / "unresolved_ids.json"
        _write_json(json_path, unresolved_rows)
        result.add(json_path)

    quality_rows = list(_iter_quality_flags(conn))
    if quality_rows:
        csv_path = export_dir / "video_quality_flags.csv"
        _write_csv(
            csv_path,
            [
                "path",
                "score",
                "reasons",
                "width",
                "height",
                "vcodec",
                "audio_max_ch",
                "subs_present",
            ],
            quality_rows,
        )
        result.add(csv_path)
        json_path = export_dir / "video_quality_flags.json"
        _write_json(json_path, quality_rows)
        result.add(json_path)

    api_path = export_dir / "api_health.json"
    _write_json(api_path, summary.api_health or {})
    result.add(api_path)

    drives_path = export_dir / "drives.json"
    _write_json(drives_path, summary.drives)
    result.add(drives_path)

    return result
