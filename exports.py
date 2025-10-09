from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, Optional

from core.paths import get_exports_dir, safe_label

CSV_COLUMNS = [
    "drive_label",
    "path",
    "size_bytes",
    "is_av",
    "hash_blake3",
    "mtime_utc",
    "codec_video",
    "width",
    "height",
    "codec_audio",
    "audio_streams",
    "video_streams",
    "duration_seconds",
    "integrity_ok",
    "deleted",
    "first_seen_utc",
    "updated_utc",
]

JSONL_FIELDS = CSV_COLUMNS

ProgressCallback = Optional[Callable[[int], None]]


@dataclass(frozen=True)
class ExportFilters:
    include_deleted: bool = False
    av_only: bool = False
    since_utc: Optional[str] = None


@dataclass(frozen=True)
class ExportResult:
    path: Path
    rows: int
    format: str


def normalize_drive_label(label: str) -> str:
    cleaned = safe_label(label)
    return cleaned or "drive"


def resolve_default_export_path(
    working_dir: Path, drive_label: str, extension: str
) -> Path:
    exports_dir = get_exports_dir(working_dir)
    exports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
    normalized = normalize_drive_label(drive_label)
    filename = f"export_{normalized}_{timestamp}{extension}"
    return exports_dir / filename


def parse_since(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # Accept trailing Z as UTC
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "--since must be an ISO 8601 timestamp (e.g. 2024-03-25T10:30:00Z)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _column_names(cursor: sqlite3.Cursor) -> set[str]:
    cursor.execute("PRAGMA table_info(files)")
    return {row[1] for row in cursor.fetchall()}


def _extract_numeric(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _extract_duration(track: Dict[str, object]) -> Optional[float]:
    duration = track.get("Duration")
    if duration is None:
        return None
    try:
        # MediaInfo reports milliseconds by default
        millis = float(duration)
        if millis <= 0:
            return None
        return round(millis / 1000.0, 3)
    except (TypeError, ValueError):
        pass
    # Sometimes duration is already seconds
    try:
        seconds = float(str(duration).strip())
        if seconds <= 0:
            return None
        return round(seconds, 3)
    except (TypeError, ValueError):
        return None


def _extract_media_details(media_blob: Optional[str]) -> Dict[str, Optional[object]]:
    details: Dict[str, Optional[object]] = {
        "codec_video": None,
        "width": None,
        "height": None,
        "codec_audio": None,
        "audio_streams": None,
        "video_streams": None,
        "duration_seconds": None,
    }
    if not media_blob:
        return details
    try:
        data = json.loads(media_blob)
    except json.JSONDecodeError:
        return details

    tracks: Iterable[Dict[str, object]] = []
    if isinstance(data, dict):
        if isinstance(data.get("media"), dict):
            maybe_tracks = data["media"].get("track")
            if isinstance(maybe_tracks, list):
                tracks = [t for t in maybe_tracks if isinstance(t, dict)]
            elif isinstance(maybe_tracks, dict):
                tracks = [maybe_tracks]
        elif isinstance(data.get("track"), list):
            tracks = [t for t in data["track"] if isinstance(t, dict)]
    if not tracks:
        return details

    audio_streams = 0
    video_streams = 0
    duration: Optional[float] = None

    for track in tracks:
        track_type = str(track.get("@type") or track.get("Type") or "").lower()
        if track_type == "video":
            video_streams += 1
            if details["codec_video"] is None:
                details["codec_video"] = (
                    track.get("Format")
                    or track.get("CodecID")
                    or track.get("CodecID/String")
                )
            if details["width"] is None:
                details["width"] = _extract_numeric(track.get("Width"))
            if details["height"] is None:
                details["height"] = _extract_numeric(track.get("Height"))
            if duration is None:
                duration = _extract_duration(track)
        elif track_type == "audio":
            audio_streams += 1
            if details["codec_audio"] is None:
                details["codec_audio"] = (
                    track.get("Format")
                    or track.get("CodecID")
                    or track.get("CodecID/String")
                )
            if duration is None:
                duration = _extract_duration(track)
        elif track_type == "general" and duration is None:
            duration = _extract_duration(track)

    if duration is not None:
        details["duration_seconds"] = duration
    details["audio_streams"] = audio_streams
    details["video_streams"] = video_streams
    if details["codec_video"] is not None:
        details["codec_video"] = str(details["codec_video"])
    if details["codec_audio"] is not None:
        details["codec_audio"] = str(details["codec_audio"])
    return details


def _row_payload(
    row: sqlite3.Row,
    available_columns: set[str],
    drive_label: str,
    has_drive_label_column: bool,
) -> Dict[str, Optional[object]]:
    deleted_value = row["deleted"] if "deleted" in available_columns else 0
    first_seen = row["first_seen_utc"] if "first_seen_utc" in available_columns else None
    updated = row["updated_utc"] if "updated_utc" in available_columns else None
    record: Dict[str, Optional[object]] = {
        "drive_label": row["drive_label"] if has_drive_label_column else drive_label,
        "path": row["path"],
        "size_bytes": row["size_bytes"],
        "is_av": 1 if row["is_av"] else 0,
        "hash_blake3": row["hash_blake3"],
        "mtime_utc": row["mtime_utc"],
        "integrity_ok": row["integrity_ok"],
        "deleted": 1 if deleted_value else 0,
        "first_seen_utc": first_seen,
        "updated_utc": updated,
    }
    media_blob = row["media_json"]
    record.update(_extract_media_details(media_blob))
    return record


def _build_query(
    available_columns: set[str],
    filters: ExportFilters,
    drive_label: str,
    has_drive_label_column: bool,
) -> tuple[str, list[object]]:
    select_columns = [
        "path",
        "size_bytes",
        "is_av",
        "hash_blake3",
        "media_json",
        "integrity_ok",
        "mtime_utc",
    ]
    for optional_col in ("deleted", "first_seen_utc", "updated_utc"):
        if optional_col in available_columns:
            select_columns.append(optional_col)
    if has_drive_label_column and "drive_label" in available_columns:
        select_columns.append("drive_label")

    query = f"SELECT {', '.join(select_columns)} FROM files"
    clauses = []
    params: list[object] = []
    if has_drive_label_column and "drive_label" in available_columns:
        clauses.append("drive_label = ?")
        params.append(drive_label)
    if filters.av_only and "is_av" in available_columns:
        clauses.append("COALESCE(is_av, 0) = 1")
    if not filters.include_deleted and "deleted" in available_columns:
        clauses.append("COALESCE(deleted, 0) = 0")
    if filters.since_utc:
        if "updated_utc" in available_columns:
            clauses.append("COALESCE(updated_utc, '') >= ?")
            params.append(filters.since_utc)
        elif "mtime_utc" in available_columns:
            clauses.append("COALESCE(mtime_utc, '') >= ?")
            params.append(filters.since_utc)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY path ASC"
    return query, params


def _stream_rows(
    connection: sqlite3.Connection,
    drive_label: str,
    filters: ExportFilters,
    has_drive_label_column: bool,
) -> Iterator[Dict[str, Optional[object]]]:
    cursor = connection.cursor()
    available_columns = _column_names(cursor)
    query, params = _build_query(available_columns, filters, drive_label, has_drive_label_column)
    cursor.execute(query, params)
    while True:
        chunk = cursor.fetchmany(500)
        if not chunk:
            break
        for row in chunk:
            if not isinstance(row, sqlite3.Row):
                row = sqlite3.Row(cursor, row)  # pragma: no cover
            yield _row_payload(row, available_columns, drive_label, has_drive_label_column)


def _format_csv_row(record: Dict[str, Optional[object]]) -> Dict[str, str]:
    formatted: Dict[str, str] = {}
    for column in CSV_COLUMNS:
        value = record.get(column)
        if column in ("is_av", "deleted"):
            formatted[column] = "1" if value else "0"
        elif column == "integrity_ok":
            if value in (0, 1):
                formatted[column] = "1" if value else "0"
            else:
                formatted[column] = ""
        elif column == "duration_seconds":
            if isinstance(value, (int, float)):
                formatted[column] = ("%0.3f" % float(value)).rstrip("0").rstrip(".")
            else:
                formatted[column] = ""
        else:
            formatted[column] = "" if value is None else str(value)
    return formatted


def _write_csv(
    rows: Iterator[Dict[str, Optional[object]]],
    output_path: Path,
    progress_callback: ProgressCallback = None,
) -> int:
    count = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in rows:
            writer.writerow(_format_csv_row(record))
            count += 1
            if progress_callback and count % 500 == 0:
                progress_callback(count)
    if progress_callback:
        progress_callback(count)
    return count


def _write_jsonl(
    rows: Iterator[Dict[str, Optional[object]]],
    output_path: Path,
    progress_callback: ProgressCallback = None,
) -> int:
    count = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        for record in rows:
            serialisable = {key: record.get(key) for key in JSONL_FIELDS}
            json.dump(serialisable, handle, ensure_ascii=False)
            handle.write("\n")
            count += 1
            if progress_callback and count % 500 == 0:
                progress_callback(count)
    if progress_callback:
        progress_callback(count)
    return count


def _export(
    db_path: Path,
    working_dir: Path,
    drive_label: str,
    filters: ExportFilters,
    fmt: str,
    output_path: Optional[Path] = None,
    has_drive_label_column: bool = False,
    progress_callback: ProgressCallback = None,
) -> ExportResult:
    suffix = ".csv" if fmt == "csv" else ".jsonl"
    if output_path is None:
        output_path = resolve_default_export_path(working_dir, drive_label, suffix)
    output_path = output_path.resolve()

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows_iter = _stream_rows(connection, drive_label, filters, has_drive_label_column)
        if fmt == "csv":
            count = _write_csv(rows_iter, output_path, progress_callback)
        elif fmt == "jsonl":
            count = _write_jsonl(rows_iter, output_path, progress_callback)
        else:
            raise ValueError(f"Unsupported export format: {fmt}")
    finally:
        connection.close()
    return ExportResult(path=output_path, rows=count, format=fmt)


def export_catalog(
    db_path: Path,
    working_dir: Path,
    drive_label: str,
    filters: ExportFilters,
    fmt: str,
    output_path: Optional[Path] = None,
    progress_callback: ProgressCallback = None,
) -> ExportResult:
    return _export(
        db_path=db_path,
        working_dir=working_dir,
        drive_label=drive_label,
        filters=filters,
        fmt=fmt,
        output_path=output_path,
        has_drive_label_column=True,
        progress_callback=progress_callback,
    )


def export_shard(
    shard_path: Path,
    working_dir: Path,
    drive_label: str,
    filters: ExportFilters,
    fmt: str,
    output_path: Optional[Path] = None,
    progress_callback: ProgressCallback = None,
) -> ExportResult:
    return _export(
        db_path=shard_path,
        working_dir=working_dir,
        drive_label=drive_label,
        filters=filters,
        fmt=fmt,
        output_path=output_path,
        has_drive_label_column=False,
        progress_callback=progress_callback,
    )
