"""Read-only SQLite helpers for the VideoCatalog local API."""
from __future__ import annotations

import base64
import binascii
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import reports_util

from core.db import connect
from core.paths import (
    get_catalog_db_path,
    get_shard_db_path,
    get_shards_dir,
    resolve_working_dir,
    safe_label,
)
from core.settings import load_settings
from semantic import (
    SemanticConfig,
    SemanticIndexer,
    SemanticPhaseError,
    SemanticSearcher,
    SemanticTranscriber,
)
from semantic.db import semantic_connection

_DEFAULT_LIMIT = 100
_MAX_PAGE_SIZE = 500
_COUNT_GUARD = 10000

_LOW_CONFIDENCE_THRESHOLD = 0.55


def _load_json_list(value: Any) -> List[str]:
    """Safely parse a JSON array into a list of strings."""

    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    results: List[str] = []
    for item in parsed:
        if item is None:
            continue
        results.append(str(item))
    return results


def _load_json_dict(value: Any) -> Dict[str, Any]:
    """Return a dictionary parsed from JSON payloads."""

    if not value:
        return {}
    try:
        payload = json.loads(value)
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _split_langs(value: Optional[str]) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    parts = str(value).replace(";", ",").split(",")
    return [part.strip() for part in parts if part.strip()]


def _extract_quality_reasons(value: Any) -> List[str]:
    if not value:
        return []
    payload: Any = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except Exception:
            payload = None
    if isinstance(payload, dict):
        return [str(key) for key, flag in payload.items() if flag]
    if isinstance(payload, list):
        return [str(item) for item in payload if str(item).strip()]
    return []


@dataclass(slots=True)
class Pagination:
    """Resolved pagination values after clamping settings and user input."""

    limit: int
    offset: int


class DataAccess:
    """Central access layer for catalog and shard databases."""

    def __init__(
        self,
        *,
        working_dir: Optional[Path] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.working_dir = Path(working_dir or resolve_working_dir())
        self._settings = dict(settings or load_settings(self.working_dir))
        self.catalog_path = self._resolve_catalog_path()
        self.shards_dir = get_shards_dir(self.working_dir)
        api_settings = (
            self._settings.get("api") if isinstance(self._settings.get("api"), dict) else {}
        )
        default_limit = int(api_settings.get("default_limit") or _DEFAULT_LIMIT)
        max_page = int(api_settings.get("max_page_size") or _MAX_PAGE_SIZE)
        if default_limit <= 0:
            default_limit = _DEFAULT_LIMIT
        if max_page <= 0:
            max_page = _MAX_PAGE_SIZE
        self.default_limit = min(default_limit, max_page)
        self.max_page_size = max_page
    def refresh_settings(self) -> None:
        """Reload settings.json from disk."""

        self._settings = load_settings(self.working_dir)
        api_settings = (
            self._settings.get("api") if isinstance(self._settings.get("api"), dict) else {}
        )
        default_limit = int(api_settings.get("default_limit") or _DEFAULT_LIMIT)
        max_page = int(api_settings.get("max_page_size") or _MAX_PAGE_SIZE)
        if default_limit <= 0:
            default_limit = _DEFAULT_LIMIT
        if max_page <= 0:
            max_page = _MAX_PAGE_SIZE
        self.default_limit = min(default_limit, max_page)
        self.max_page_size = max_page

    # ------------------------------------------------------------------
    # Catalog helpers
    # ------------------------------------------------------------------

    def _iter_drive_labels(self) -> Iterator[str]:
        """Yield all known drive labels registered in the catalog database."""

        try:
            drives = self.list_drives()
        except Exception:
            drives = []
        seen: set[str] = set()
        for drive in drives:
            label = drive.get("label") if isinstance(drive, dict) else None
            if not label:
                continue
            if label in seen:
                continue
            seen.add(label)
            yield str(label)

    def _iter_shards_with_labels(self) -> Iterator[Tuple[str, Path]]:
        """Yield ``(drive_label, shard_path)`` tuples for available shard databases."""

        shards_dir = self.shards_dir
        known_labels = list(self._iter_drive_labels())
        for label in known_labels:
            path = get_shard_db_path(self.working_dir, label)
            if path.exists():
                yield label, path
        # Fall back to discovering loose shard files when drives table is empty
        try:
            for candidate in sorted(shards_dir.glob("*.db")):
                label = candidate.stem
                try:
                    decoded = label.encode("utf-8", errors="ignore").decode("utf-8")
                except Exception:
                    decoded = label
                if decoded in known_labels:
                    continue
                yield decoded, candidate
        except FileNotFoundError:
            return

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> set[str]:
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        except sqlite3.DatabaseError:
            return set()
        return {str(row[0]) for row in cursor.fetchall() if row[0]}

    @staticmethod
    def _encode_media_token(
        drive_label: str,
        item_type: str,
        item_key: str,
        *,
        variant: str = "thumb",
    ) -> str:
        payload = json.dumps(
            {
                "drive": drive_label,
                "type": item_type,
                "key": item_key,
                "variant": variant,
            },
            separators=(",", ":"),
        ).encode("utf-8", "ignore")
        token = base64.urlsafe_b64encode(payload).decode("ascii", "ignore")
        return token.rstrip("=")

    @staticmethod
    def _decode_media_token(token: str) -> Optional[Dict[str, str]]:
        if not token:
            return None
        padding = "=" * (-len(token) % 4)
        try:
            decoded = base64.urlsafe_b64decode(token + padding)
        except (ValueError, binascii.Error):  # type: ignore[name-defined]
            return None
        try:
            payload = json.loads(decoded.decode("utf-8", "ignore"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        drive = payload.get("drive")
        item_type = payload.get("type")
        item_key = payload.get("key")
        variant = payload.get("variant") or "thumb"
        if not all(isinstance(value, str) and value for value in (drive, item_type, item_key)):
            return None
        return {
            "drive": drive,
            "type": item_type,
            "key": item_key,
            "variant": str(variant),
        }

    @staticmethod
    def _parse_item_id(item_id: str) -> Optional[Tuple[str, str, str]]:
        if not item_id:
            return None
        parts = item_id.split(":", 2)
        if len(parts) != 3:
            return None
        kind, drive, key = parts
        if not all(parts):
            return None
        return kind, drive, key

    # ------------------------------------------------------------------
    # Data extraction helpers
    # ------------------------------------------------------------------

    def _movies_from_shard(
        self, conn: sqlite3.Connection, drive_label: str
    ) -> List[Dict[str, Any]]:
        tables = self._table_names(conn)
        if "folder_profile" not in tables:
            return []
        joins = []
        columns = [
            "fp.folder_path",
            "fp.parsed_title",
            "fp.parsed_year",
            "fp.confidence",
            "fp.assets_json",
            "fp.source_signals_json",
            "fp.issues_json",
            "fp.main_video_path",
            "fp.updated_utc",
        ]
        thumb_keys: set[str] = set()
        sheet_keys: set[str] = set()
        if "video_thumbs" in tables:
            try:
                cursor = conn.execute(
                    "SELECT item_key FROM video_thumbs WHERE item_type IN ('folder','movie')"
                )
                thumb_keys = {str(row[0]) for row in cursor.fetchall() if row[0]}
            except sqlite3.DatabaseError:
                thumb_keys = set()
        if "contact_sheets" in tables:
            try:
                cursor = conn.execute(
                    "SELECT item_key FROM contact_sheets WHERE item_type IN ('folder','movie')"
                )
                sheet_keys = {str(row[0]) for row in cursor.fetchall() if row[0]}
            except sqlite3.DatabaseError:
                sheet_keys = set()
        if "video_quality" in tables:
            columns.extend(
                [
                    "q.score AS quality_score",
                    "q.audio_langs",
                    "q.subs_langs",
                    "q.subs_present",
                    "q.reasons_json AS quality_reasons",
                    "q.duration_s",
                    "q.video_codec",
                    "q.width",
                    "q.height",
                ]
            )
            joins.append("LEFT JOIN video_quality AS q ON q.path = fp.main_video_path")
        else:
            columns.extend(
                [
                    "NULL AS quality_score",
                    "NULL AS audio_langs",
                    "NULL AS subs_langs",
                    "NULL AS subs_present",
                    "NULL AS quality_reasons",
                    "NULL AS duration_s",
                    "NULL AS video_codec",
                    "NULL AS width",
                    "NULL AS height",
                ]
            )
        if "review_queue" in tables:
            columns.append("rq.reasons_json AS review_reasons")
            joins.append("LEFT JOIN review_queue AS rq ON rq.folder_path = fp.folder_path")
        else:
            columns.append("NULL AS review_reasons")
        sql = (
            "SELECT "
            + ",".join(columns)
            + " FROM folder_profile AS fp "
            + " ".join(joins)
            + " WHERE COALESCE(LOWER(fp.kind),'') IN ('', 'movie')"
        )
        try:
            cursor = conn.execute(sql)
        except sqlite3.DatabaseError:
            return []
        results: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            folder_path = row["folder_path"]
            title = row["parsed_title"] or Path(folder_path).name
            audio_langs = _split_langs(row["audio_langs"]) if "audio_langs" in row.keys() else []
            subs_langs = _split_langs(row["subs_langs"]) if "subs_langs" in row.keys() else []
            quality_reasons = _extract_quality_reasons(row["quality_reasons"]) if "quality_reasons" in row.keys() else []
            try:
                subs_present = bool(row["subs_present"])
            except Exception:
                subs_present = False
            assets = _load_json_dict(row["assets_json"])
            signals = _load_json_dict(row["source_signals_json"])
            review_reasons = _load_json_list(row["review_reasons"]) if row["review_reasons"] else []
            results.append(
                {
                    "id": f"movie:{drive_label}:{folder_path}",
                    "folder_path": folder_path,
                    "title": title,
                    "year": row["parsed_year"],
                    "confidence": float(row["confidence"] or 0.0),
                    "assets": assets,
                    "signals": signals,
                    "quality_score": row["quality_score"],
                    "audio_langs": audio_langs,
                    "subs_langs": subs_langs,
                    "subs_present": subs_present,
                    "quality_reasons": quality_reasons,
                    "duration_s": row["duration_s"],
                    "video_codec": row["video_codec"],
                    "width": row["width"],
                    "height": row["height"],
                    "drive": drive_label,
                    "main_video_path": row["main_video_path"],
                    "updated_utc": row["updated_utc"],
                    "review_reasons": review_reasons,
                    "has_thumb": folder_path in thumb_keys,
                    "has_sheet": folder_path in sheet_keys,
                }
            )
        return results

    # ------------------------------------------------------------------
    # Public catalog API
    # ------------------------------------------------------------------

    def catalog_movies_page(
        self,
        *,
        query: Optional[str] = None,
        year_min: Optional[int] = None,
        year_max: Optional[int] = None,
        confidence_min: Optional[float] = None,
        quality_min: Optional[int] = None,
        audio_langs: Optional[Sequence[str]] = None,
        subs_langs: Optional[Sequence[str]] = None,
        drive: Optional[str] = None,
        only_low_confidence: bool = False,
        sort: str = "title",
        order: str = "asc",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        items: List[Dict[str, Any]] = []
        requested_drive = drive.strip() if isinstance(drive, str) and drive.strip() else None
        audio_filters = [lang.lower() for lang in audio_langs or [] if lang]
        subs_filters = [lang.lower() for lang in subs_langs or [] if lang]
        for label, shard_path in self._iter_shards_with_labels():
            if requested_drive and label != requested_drive:
                continue
            try:
                conn = self._connect(shard_path)
            except Exception:
                continue
            try:
                shard_movies = self._movies_from_shard(conn, label)
            finally:
                conn.close()
            items.extend(shard_movies)

        if not items:
            return [], pagination, None, 0

        def matches(row: Dict[str, Any]) -> bool:
            if query:
                token = query.lower()
                haystack = " ".join(
                    [
                        str(row.get("title") or ""),
                        str(row.get("folder_path") or ""),
                    ]
                ).lower()
                if token not in haystack:
                    return False
            if year_min is not None:
                try:
                    if row.get("year") and int(row["year"]) < int(year_min):
                        return False
                except Exception:
                    pass
            if year_max is not None:
                try:
                    if row.get("year") and int(row["year"]) > int(year_max):
                        return False
                except Exception:
                    pass
            if confidence_min is not None and row.get("confidence") is not None:
                if float(row["confidence"]) < float(confidence_min):
                    return False
            if only_low_confidence and float(row.get("confidence") or 0.0) >= _LOW_CONFIDENCE_THRESHOLD:
                return False
            if quality_min is not None and row.get("quality_score") is not None:
                try:
                    if int(row["quality_score"]) < int(quality_min):
                        return False
                except Exception:
                    pass
            if audio_filters:
                langs = [lang.lower() for lang in row.get("audio_langs") or []]
                if not all(any(f == lang for lang in langs) for f in audio_filters):
                    return False
            if subs_filters:
                langs = [lang.lower() for lang in row.get("subs_langs") or []]
                if not all(any(f == lang for lang in langs) for f in subs_filters):
                    return False
            return True

        filtered = [row for row in items if matches(row)]

        sort_key = sort.lower() if sort else "title"
        reverse = str(order or "asc").lower() == "desc"

        def key_func(row: Dict[str, Any]):
            if sort_key == "year":
                return row.get("year") or 0
            if sort_key == "confidence":
                return row.get("confidence") or 0.0
            if sort_key == "quality":
                return row.get("quality_score") or -1
            # Default title sort uses casefold for stability
            return str(row.get("title") or "").casefold()

        filtered.sort(key=key_func, reverse=reverse)
        total = len(filtered)
        start = pagination.offset
        end = start + pagination.limit
        page_rows = filtered[start:end]
        next_offset = start + pagination.limit if end < total else None

        results: List[Dict[str, Any]] = []
        for row in page_rows:
            thumb_token = (
                self._encode_media_token(row["drive"], "folder", row["folder_path"], variant="thumb")
                if row.get("has_thumb")
                else None
            )
            sheet_token = (
                self._encode_media_token(row["drive"], "folder", row["folder_path"], variant="sheet")
                if row.get("has_sheet")
                else None
            )
            results.append(
                {
                    "id": row["id"],
                    "path": row["folder_path"],
                    "title": row.get("title"),
                    "year": row.get("year"),
                    "poster_thumb": thumb_token,
                    "contact_sheet": sheet_token,
                    "confidence": float(row.get("confidence") or 0.0),
                    "quality": row.get("quality_score"),
                    "langs_audio": row.get("audio_langs") or [],
                    "langs_subs": row.get("subs_langs") or [],
                    "drive": row.get("drive"),
                }
            )
        return results, pagination, next_offset, total

    def catalog_fetch_media_blob(self, token: str) -> Optional[Tuple[bytes, str]]:
        payload = self._decode_media_token(token)
        if not payload:
            return None
        drive = payload["drive"]
        item_type = payload["type"]
        item_key = payload["key"]
        variant = payload.get("variant", "thumb")
        try:
            with self._shard(drive) as conn:
                table = "video_thumbs" if variant == "thumb" else "contact_sheets"
                column = "image_blob"
                format_col = "format"
                query = (
                    f"SELECT {format_col}, {column} FROM {table} "
                    "WHERE item_type = ? AND item_key = ? LIMIT 1"
                )
                cursor = conn.execute(query, (item_type, item_key))
                row = cursor.fetchone()
                if row is None or not row[column]:
                    return None
                fmt = str(row[format_col] or "JPEG").lower()
                blob = bytes(row[column])
        except sqlite3.DatabaseError:
            return None
        mime = "image/jpeg"
        if fmt in {"png", "image/png"}:
            mime = "image/png"
        elif fmt in {"webp", "image/webp"}:
            mime = "image/webp"
        elif fmt in {"avif", "image/avif"}:
            mime = "image/avif"
        return blob, mime

    def _tv_series_from_shard(
        self, conn: sqlite3.Connection, drive_label: str
    ) -> List[Dict[str, Any]]:
        tables = self._table_names(conn)
        if "tv_series_profile" not in tables:
            return []
        thumbs: set[str] = set()
        if "video_thumbs" in tables:
            try:
                rows = conn.execute(
                    "SELECT item_key FROM video_thumbs WHERE item_type='series'"
                ).fetchall()
                thumbs = {str(row[0]) for row in rows if row[0]}
            except sqlite3.DatabaseError:
                thumbs = set()
        try:
            cursor = conn.execute(
                """
                SELECT series_root, show_title, show_year, ids_json, confidence,
                       assets_json, issues_json, seasons_found, updated_utc
                FROM tv_series_profile
                """
            )
        except sqlite3.DatabaseError:
            return []
        results: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            series_root = row["series_root"]
            title = row["show_title"] or Path(series_root).name
            ids = _load_json_dict(row["ids_json"])
            assets = _load_json_dict(row["assets_json"])
            results.append(
                {
                    "id": f"series:{drive_label}:{series_root}",
                    "series_root": series_root,
                    "title": title,
                    "year": row["show_year"],
                    "confidence": float(row["confidence"] or 0.0),
                    "ids": ids,
                    "assets": assets,
                    "issues": _load_json_list(row["issues_json"]),
                    "seasons_found": row["seasons_found"],
                    "drive": drive_label,
                    "updated_utc": row["updated_utc"],
                    "has_thumb": series_root in thumbs,
                }
            )
        return results

    def _tv_seasons_from_shard(
        self, conn: sqlite3.Connection, drive_label: str, series_root: str
    ) -> List[Dict[str, Any]]:
        tables = self._table_names(conn)
        if "tv_season_profile" not in tables:
            return []
        try:
            cursor = conn.execute(
                """
                SELECT season_path, season_number, episodes_found, expected_episodes,
                       confidence, assets_json, issues_json, updated_utc
                FROM tv_season_profile
                WHERE series_root = ?
                ORDER BY season_number ASC
                """,
                (series_root,),
            )
        except sqlite3.DatabaseError:
            return []
        rows = []
        for row in cursor.fetchall():
            season_path = row["season_path"]
            rows.append(
                {
                    "id": f"season:{drive_label}:{season_path}",
                    "season_path": season_path,
                    "season_number": row["season_number"],
                    "episodes_found": row["episodes_found"],
                    "expected": row["expected_episodes"],
                    "confidence": float(row["confidence"] or 0.0),
                    "assets": _load_json_dict(row["assets_json"]),
                    "issues": _load_json_list(row["issues_json"]),
                    "drive": drive_label,
                    "updated_utc": row["updated_utc"],
                }
            )
        return rows

    def _tv_episodes_from_shard(
        self,
        conn: sqlite3.Connection,
        drive_label: str,
        series_root: str,
        season_number: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        tables = self._table_names(conn)
        if "tv_episode_profile" not in tables:
            return []
        thumbs: set[str] = set()
        if "video_thumbs" in tables:
            try:
                rows = conn.execute(
                    "SELECT item_key FROM video_thumbs WHERE item_type='episode'"
                ).fetchall()
                thumbs = {str(row[0]) for row in rows if row[0]}
            except sqlite3.DatabaseError:
                thumbs = set()
        joins = []
        select = [
            "episode_path",
            "season_number",
            "episode_numbers_json",
            "air_date",
            "parsed_title",
            "ids_json",
            "subtitles_json",
            "audio_langs_json",
            "issues_json",
            "confidence",
            "updated_utc",
        ]
        if "video_quality" in tables:
            select.extend(
                [
                    "q.score AS quality_score",
                    "q.audio_langs",
                    "q.subs_langs",
                    "q.subs_present",
                    "q.reasons_json AS quality_reasons",
                ]
            )
            joins.append("LEFT JOIN video_quality AS q ON q.path = ep.episode_path")
        else:
            select.extend(
                [
                    "NULL AS quality_score",
                    "NULL AS audio_langs",
                    "NULL AS subs_langs",
                    "NULL AS subs_present",
                    "NULL AS quality_reasons",
                ]
            )
        sql = (
            "SELECT "
            + ",".join(select)
            + " FROM tv_episode_profile AS ep "
            + " ".join(joins)
            + " WHERE ep.series_root = ?"
        )
        params: List[Any] = [series_root]
        if season_number is not None:
            sql += " AND ep.season_number = ?"
            params.append(int(season_number))
        sql += " ORDER BY ep.season_number ASC, ep.updated_utc DESC"
        try:
            cursor = conn.execute(sql, params)
        except sqlite3.DatabaseError:
            return []
        results: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            episode_path = row["episode_path"]
            numbers = _load_json_list(row["episode_numbers_json"])
            ids = _load_json_dict(row["ids_json"])
            audio = _split_langs(row["audio_langs"]) if "audio_langs" in row.keys() else []
            subs = _split_langs(row["subs_langs"]) if "subs_langs" in row.keys() else []
            subs_present = bool(row["subs_present"]) if "subs_present" in row.keys() else False
            quality_reasons = _extract_quality_reasons(row["quality_reasons"]) if row["quality_reasons"] else []
            results.append(
                {
                    "id": f"episode:{drive_label}:{episode_path}",
                    "episode_path": episode_path,
                    "season_number": row["season_number"],
                    "episode_numbers": numbers,
                    "air_date": row["air_date"],
                    "title": row["parsed_title"] or Path(episode_path).stem,
                    "ids": ids,
                    "subs": _load_json_list(row["subtitles_json"]),
                    "audio_langs": audio,
                    "subs_langs": subs,
                    "subs_present": subs_present,
                    "quality_score": row["quality_score"],
                    "quality_reasons": quality_reasons,
                    "confidence": float(row["confidence"] or 0.0),
                    "drive": drive_label,
                    "updated_utc": row["updated_utc"],
                    "has_thumb": episode_path in thumbs,
                }
            )
        return results

    def catalog_tv_series_page(
        self,
        *,
        query: Optional[str] = None,
        confidence_min: Optional[float] = None,
        drive: Optional[str] = None,
        only_low_confidence: bool = False,
        sort: str = "title",
        order: str = "asc",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        requested_drive = drive.strip() if isinstance(drive, str) and drive.strip() else None
        items: List[Dict[str, Any]] = []
        for label, shard_path in self._iter_shards_with_labels():
            if requested_drive and label != requested_drive:
                continue
            try:
                conn = self._connect(shard_path)
            except Exception:
                continue
            try:
                items.extend(self._tv_series_from_shard(conn, label))
            finally:
                conn.close()

        if not items:
            return [], pagination, None, 0

        def matches(row: Dict[str, Any]) -> bool:
            if query:
                token = query.lower()
                haystack = " ".join(
                    [
                        str(row.get("title") or ""),
                        str(row.get("series_root") or ""),
                    ]
                ).lower()
                if token not in haystack:
                    return False
            if confidence_min is not None and row.get("confidence") is not None:
                if float(row["confidence"]) < float(confidence_min):
                    return False
            if only_low_confidence and float(row.get("confidence") or 0.0) >= _LOW_CONFIDENCE_THRESHOLD:
                return False
            return True

        filtered = [row for row in items if matches(row)]
        sort_key = sort.lower() if sort else "title"
        reverse = str(order or "asc").lower() == "desc"

        def key_func(row: Dict[str, Any]):
            if sort_key == "confidence":
                return row.get("confidence") or 0.0
            if sort_key == "seasons":
                return row.get("seasons_found") or 0
            return str(row.get("title") or "").casefold()

        filtered.sort(key=key_func, reverse=reverse)
        total = len(filtered)
        start = pagination.offset
        end = start + pagination.limit
        page_rows = filtered[start:end]
        next_offset = start + pagination.limit if end < total else None

        results: List[Dict[str, Any]] = []
        for row in page_rows:
            thumb_token = (
                self._encode_media_token(row["drive"], "series", row["series_root"], variant="thumb")
                if row.get("has_thumb")
                else None
            )
            results.append(
                {
                    "id": row["id"],
                    "series_root": row["series_root"],
                    "title": row.get("title"),
                    "year": row.get("year"),
                    "confidence": float(row.get("confidence") or 0.0),
                    "seasons_found": row.get("seasons_found") or 0,
                    "poster_thumb": thumb_token,
                    "drive": row.get("drive"),
                }
            )
        return results, pagination, next_offset, total

    def catalog_tv_seasons(
        self, series_id: str
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        parsed = self._parse_item_id(series_id)
        if not parsed or parsed[0] != "series":
            raise LookupError("invalid series id")
        _, drive, series_root = parsed
        try:
            with self._shard(drive) as conn:
                series_rows = self._tv_series_from_shard(conn, drive)
                seasons = self._tv_seasons_from_shard(conn, drive, series_root)
        except (LookupError, FileNotFoundError):
            raise
        except Exception:
            return [], {}
        series_meta = next((row for row in series_rows if row.get("series_root") == series_root), None)
        meta = series_meta or {
            "id": f"series:{drive}:{series_root}",
            "title": Path(series_root).name,
            "drive": drive,
        }
        return seasons, meta

    def catalog_tv_episodes(
        self, series_id: str, *, season_number: Optional[int] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        parsed = self._parse_item_id(series_id)
        if not parsed or parsed[0] != "series":
            raise LookupError("invalid series id")
        _, drive, series_root = parsed
        try:
            with self._shard(drive) as conn:
                episodes = self._tv_episodes_from_shard(
                    conn, drive, series_root, season_number
                )
                seasons = self._tv_seasons_from_shard(conn, drive, series_root)
        except (LookupError, FileNotFoundError):
            raise
        except Exception:
            return [], {}
        season_meta: Optional[Dict[str, Any]] = None
        if season_number is not None:
            for season in seasons:
                if season.get("season_number") == season_number:
                    season_meta = season
                    break
        return episodes, season_meta or {}

    def catalog_movie_detail(self, drive: str, folder_path: str) -> Optional[Dict[str, Any]]:
        try:
            with self._shard(drive) as conn:
                tables = self._table_names(conn)
                if "folder_profile" not in tables:
                    return None
                joins = []
                columns = [
                    "fp.folder_path",
                    "fp.main_video_path",
                    "fp.parsed_title",
                    "fp.parsed_year",
                    "fp.assets_json",
                    "fp.source_signals_json",
                    "fp.issues_json",
                    "fp.confidence",
                    "fp.updated_utc",
                ]
                if "video_quality" in tables:
                    columns.extend(
                        [
                            "q.score AS quality_score",
                            "q.audio_langs",
                            "q.subs_langs",
                            "q.subs_present",
                            "q.reasons_json AS quality_reasons",
                            "q.duration_s",
                            "q.width",
                            "q.height",
                            "q.video_codec",
                            "q.container",
                        ]
                    )
                    joins.append("LEFT JOIN video_quality AS q ON q.path = fp.main_video_path")
                else:
                    columns.extend(
                        [
                            "NULL AS quality_score",
                            "NULL AS audio_langs",
                            "NULL AS subs_langs",
                            "NULL AS subs_present",
                            "NULL AS quality_reasons",
                            "NULL AS duration_s",
                            "NULL AS width",
                            "NULL AS height",
                            "NULL AS video_codec",
                            "NULL AS container",
                        ]
                    )
                if "review_queue" in tables:
                    columns.extend(
                        [
                            "rq.reasons_json AS review_reasons",
                            "rq.questions_json AS review_questions",
                        ]
                    )
                    joins.append("LEFT JOIN review_queue AS rq ON rq.folder_path = fp.folder_path")
                else:
                    columns.extend(["NULL AS review_reasons", "NULL AS review_questions"])
                sql = (
                    "SELECT "
                    + ",".join(columns)
                    + " FROM folder_profile AS fp "
                    + " ".join(joins)
                    + " WHERE fp.folder_path = ? LIMIT 1"
                )
                cursor = conn.execute(sql, (folder_path,))
                row = cursor.fetchone()
                if row is None:
                    return None
                candidates: List[Dict[str, Any]] = []
                if "folder_candidates" in tables:
                    try:
                        cand_cursor = conn.execute(
                            """
                            SELECT source, candidate_id, title, year, score, extra_json
                            FROM folder_candidates
                            WHERE folder_path = ?
                            ORDER BY score DESC
                            """,
                            (folder_path,),
                        )
                        for cand in cand_cursor.fetchall():
                            extra = _load_json_dict(cand["extra_json"])
                            candidates.append(
                                {
                                    "source": cand["source"],
                                    "candidate_id": cand["candidate_id"],
                                    "title": cand["title"],
                                    "year": cand["year"],
                                    "score": float(cand["score"] or 0.0),
                                    "extra": extra,
                                }
                            )
                    except sqlite3.DatabaseError:
                        candidates = []
        except (LookupError, FileNotFoundError):
            raise
        except Exception:
            return None
        assets = _load_json_dict(row["assets_json"])
        signals = _load_json_dict(row["source_signals_json"])
        issues = _load_json_list(row["issues_json"])
        quality_reasons = _extract_quality_reasons(row["quality_reasons"]) if row["quality_reasons"] else []
        review_reasons = _load_json_list(row["review_reasons"]) if row["review_reasons"] else []
        review_questions = _load_json_list(row["review_questions"]) if row["review_questions"] else []
        confidence_components: List[Dict[str, Any]] = []
        for key in ("confidence_components", "signals", "components"):
            if isinstance(signals.get(key), dict):
                for name, value in signals[key].items():
                    try:
                        confidence_components.append(
                            {"label": str(name), "score": float(value)}
                        )
                    except Exception:
                        continue
                break
        overview = None
        for candidate in (
            assets.get("plot"),
            assets.get("overview"),
            signals.get("overview"),
            signals.get("synopsis"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                overview = candidate.strip()
                break
        runtime_minutes = None
        try:
            duration_s = float(row["duration_s"]) if row["duration_s"] else None
            if duration_s:
                runtime_minutes = int(round(duration_s / 60))
        except Exception:
            runtime_minutes = None
        thumb_token = self._encode_media_token(drive, "folder", folder_path, variant="thumb")
        sheet_token = self._encode_media_token(drive, "folder", folder_path, variant="sheet")
        ids: Dict[str, Any] = {}
        for source in (assets, signals):
            if isinstance(source.get("ids"), dict):
                for key, value in source["ids"].items():
                    if value is None:
                        continue
                    ids[str(key)] = value
            for key in ("tmdb", "tmdb_id", "imdb", "imdb_id", "tvdb", "tvdb_id"):
                value = source.get(key)
                if value is None:
                    continue
                ids[key] = value
        return {
            "id": f"movie:{drive}:{folder_path}",
            "title": row["parsed_title"] or Path(folder_path).name,
            "year": row["parsed_year"],
            "drive": drive,
            "folder_path": folder_path,
            "main_video_path": row["main_video_path"],
            "runtime_minutes": runtime_minutes,
            "overview": overview,
            "confidence": float(row["confidence"] or 0.0),
            "confidence_components": confidence_components,
            "quality_score": row["quality_score"],
            "quality_reasons": quality_reasons,
            "audio_langs": _split_langs(row["audio_langs"]) if "audio_langs" in row.keys() else [],
            "subs_langs": _split_langs(row["subs_langs"]) if "subs_langs" in row.keys() else [],
            "subs_present": bool(row["subs_present"]) if "subs_present" in row.keys() else False,
            "video_codec": row["video_codec"],
            "resolution": {
                "width": row["width"] if "width" in row.keys() else None,
                "height": row["height"] if "height" in row.keys() else None,
            },
            "container": row["container"] if "container" in row.keys() else None,
            "ids": ids,
            "assets": assets,
            "signals": signals,
            "issues": issues,
            "candidates": candidates,
            "review_reasons": review_reasons,
            "review_questions": review_questions,
            "poster_thumb": thumb_token,
            "contact_sheet": sheet_token,
            "open_plan": {"plan": "shell_open", "path": folder_path},
        }

    def catalog_episode_detail(
        self, drive: str, episode_path: str
    ) -> Optional[Dict[str, Any]]:
        try:
            with self._shard(drive) as conn:
                tables = self._table_names(conn)
                if "tv_episode_profile" not in tables:
                    return None
                joins = []
                columns = [
                    "ep.episode_path",
                    "ep.series_root",
                    "ep.season_number",
                    "ep.episode_numbers_json",
                    "ep.air_date",
                    "ep.parsed_title",
                    "ep.ids_json",
                    "ep.subtitles_json",
                    "ep.audio_langs_json",
                    "ep.issues_json",
                    "ep.confidence",
                    "ep.updated_utc",
                ]
                if "video_quality" in tables:
                    columns.extend(
                        [
                            "q.score AS quality_score",
                            "q.audio_langs",
                            "q.subs_langs",
                            "q.subs_present",
                            "q.reasons_json AS quality_reasons",
                            "q.duration_s",
                            "q.width",
                            "q.height",
                            "q.video_codec",
                        ]
                    )
                    joins.append("LEFT JOIN video_quality AS q ON q.path = ep.episode_path")
                else:
                    columns.extend(
                        [
                            "NULL AS quality_score",
                            "NULL AS audio_langs",
                            "NULL AS subs_langs",
                            "NULL AS subs_present",
                            "NULL AS quality_reasons",
                            "NULL AS duration_s",
                            "NULL AS width",
                            "NULL AS height",
                            "NULL AS video_codec",
                        ]
                    )
                sql = (
                    "SELECT "
                    + ",".join(columns)
                    + " FROM tv_episode_profile AS ep "
                    + " ".join(joins)
                    + " WHERE ep.episode_path = ? LIMIT 1"
                )
                cursor = conn.execute(sql, (episode_path,))
                row = cursor.fetchone()
                if row is None:
                    return None
                series = self._tv_series_from_shard(conn, drive)
                seasons = self._tv_seasons_from_shard(conn, drive, row["series_root"])
        except (LookupError, FileNotFoundError):
            raise
        except Exception:
            return None
        ids = _load_json_dict(row["ids_json"])
        audio_langs = _split_langs(row["audio_langs"]) if "audio_langs" in row.keys() else []
        subs_langs = _split_langs(row["subs_langs"]) if "subs_langs" in row.keys() else []
        subs_present = bool(row["subs_present"]) if "subs_present" in row.keys() else False
        quality_reasons = _extract_quality_reasons(row["quality_reasons"]) if row["quality_reasons"] else []
        series_meta = next((s for s in series if s.get("series_root") == row["series_root"]), None)
        season_meta = next(
            (s for s in seasons if s.get("season_number") == row["season_number"]),
            None,
        )
        runtime_minutes = None
        try:
            duration_s = float(row["duration_s"]) if row["duration_s"] else None
            if duration_s:
                runtime_minutes = int(round(duration_s / 60))
        except Exception:
            runtime_minutes = None
        thumb_token = self._encode_media_token(drive, "episode", episode_path, variant="thumb")
        return {
            "id": f"episode:{drive}:{episode_path}",
            "title": row["parsed_title"] or Path(episode_path).stem,
            "drive": drive,
            "episode_path": episode_path,
            "series_root": row["series_root"],
            "season_number": row["season_number"],
            "episode_numbers": _load_json_list(row["episode_numbers_json"]),
            "air_date": row["air_date"],
            "ids": ids,
            "confidence": float(row["confidence"] or 0.0),
            "audio_langs": audio_langs,
            "subs_langs": subs_langs,
            "subs_present": subs_present,
            "quality_score": row["quality_score"],
            "quality_reasons": quality_reasons,
            "runtime_minutes": runtime_minutes,
            "video_codec": row["video_codec"],
            "resolution": {"width": row["width"], "height": row["height"]},
            "series": series_meta or {},
            "season": season_meta or {},
            "poster_thumb": thumb_token,
            "open_plan": {"plan": "shell_open", "path": Path(episode_path).parent.as_posix()},
        }

    def catalog_item_detail(self, item_id: str) -> Optional[Dict[str, Any]]:
        parsed = self._parse_item_id(item_id)
        if not parsed:
            return None
        kind, drive, key = parsed
        if kind == "movie":
            return self.catalog_movie_detail(drive, key)
        if kind == "episode":
            return self.catalog_episode_detail(drive, key)
        if kind == "series":
            seasons, meta = self.catalog_tv_seasons(item_id)
            return {
                "id": item_id,
                "kind": "series",
                "drive": meta.get("drive"),
                "title": meta.get("title"),
                "seasons": seasons,
            }
        return None

    def catalog_summary(self, *, review_limit: int = 8) -> Dict[str, Any]:
        movies_total = 0
        series_total = 0
        episodes_total = 0
        low_conf_movies: List[Dict[str, Any]] = []
        low_conf_episodes: List[Dict[str, Any]] = []
        for drive, shard_path in self._iter_shards_with_labels():
            try:
                conn = self._connect(shard_path)
            except Exception:
                continue
            try:
                tables = self._table_names(conn)
                if "folder_profile" in tables:
                    try:
                        movies_total += int(
                            conn.execute(
                                "SELECT COUNT(*) FROM folder_profile WHERE COALESCE(LOWER(kind),'') IN ('','movie')"
                            ).fetchone()[0]
                        )
                        rows = conn.execute(
                            """
                            SELECT folder_path, parsed_title, parsed_year, confidence
                            FROM folder_profile
                            WHERE COALESCE(LOWER(kind),'') IN ('','movie')
                            ORDER BY confidence ASC, updated_utc DESC
                            LIMIT ?
                            """,
                            (review_limit,),
                        ).fetchall()
                        for row in rows:
                            low_conf_movies.append(
                                {
                                    "id": f"movie:{drive}:{row['folder_path']}",
                                    "title": row["parsed_title"] or Path(row["folder_path"]).name,
                                    "year": row["parsed_year"],
                                    "confidence": float(row["confidence"] or 0.0),
                                    "drive": drive,
                                }
                            )
                    except sqlite3.DatabaseError:
                        pass
                if "tv_series_profile" in tables:
                    try:
                        series_total += int(
                            conn.execute("SELECT COUNT(*) FROM tv_series_profile").fetchone()[0]
                        )
                    except sqlite3.DatabaseError:
                        pass
                if "tv_episode_profile" in tables:
                    try:
                        episodes_total += int(
                            conn.execute("SELECT COUNT(*) FROM tv_episode_profile").fetchone()[0]
                        )
                        rows = conn.execute(
                            """
                            SELECT episode_path, parsed_title, confidence, season_number, episode_numbers_json
                            FROM tv_episode_profile
                            ORDER BY confidence ASC, updated_utc DESC
                            LIMIT ?
                            """,
                            (review_limit,),
                        ).fetchall()
                        for row in rows:
                            numbers = _load_json_list(row["episode_numbers_json"])
                            low_conf_episodes.append(
                                {
                                    "id": f"episode:{drive}:{row['episode_path']}",
                                    "title": row["parsed_title"] or Path(row["episode_path"]).stem,
                                    "confidence": float(row["confidence"] or 0.0),
                                    "season": row["season_number"],
                                    "episode_numbers": numbers,
                                    "drive": drive,
                                }
                            )
                    except sqlite3.DatabaseError:
                        pass
            finally:
                conn.close()

        low_conf_movies.sort(key=lambda item: item.get("confidence", 0.0))
        low_conf_episodes.sort(key=lambda item: item.get("confidence", 0.0))
        return {
            "totals": {
                "movies": movies_total,
                "series": series_total,
                "episodes": episodes_total,
            },
            "review_queue": {
                "movies": low_conf_movies[:review_limit],
                "episodes": low_conf_episodes[:review_limit],
            },
        }

    def catalog_search(
        self,
        query: str,
        *,
        mode: str = "fts",
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        token = query.strip().lower()
        if not token:
            return []
        like_pattern = f"%{token}%"
        hits: List[Dict[str, Any]] = []
        for drive, shard_path in self._iter_shards_with_labels():
            try:
                conn = self._connect(shard_path)
            except Exception:
                continue
            try:
                tables = self._table_names(conn)
                if "folder_profile" in tables:
                    try:
                        rows = conn.execute(
                            """
                            SELECT folder_path, parsed_title, parsed_year, confidence
                            FROM folder_profile
                            WHERE COALESCE(LOWER(kind),'') IN ('','movie')
                              AND (
                                LOWER(COALESCE(parsed_title,'')) LIKE ?
                                OR LOWER(folder_path) LIKE ?
                              )
                            ORDER BY confidence DESC, updated_utc DESC
                            LIMIT ?
                            """,
                            (like_pattern, like_pattern, top_k),
                        ).fetchall()
                        for row in rows:
                            hits.append(
                                {
                                    "id": f"movie:{drive}:{row['folder_path']}",
                                    "kind": "movie",
                                    "title": row["parsed_title"] or Path(row["folder_path"]).name,
                                    "drive": drive,
                                    "confidence": float(row["confidence"] or 0.0),
                                    "context": {
                                        "year": row["parsed_year"],
                                    },
                                }
                            )
                    except sqlite3.DatabaseError:
                        pass
                if "tv_episode_profile" in tables:
                    try:
                        rows = conn.execute(
                            """
                            SELECT episode_path, parsed_title, season_number, episode_numbers_json, confidence
                            FROM tv_episode_profile
                            WHERE LOWER(COALESCE(parsed_title,'')) LIKE ?
                               OR LOWER(episode_path) LIKE ?
                            ORDER BY confidence DESC, updated_utc DESC
                            LIMIT ?
                            """,
                            (like_pattern, like_pattern, top_k),
                        ).fetchall()
                        for row in rows:
                            hits.append(
                                {
                                    "id": f"episode:{drive}:{row['episode_path']}",
                                    "kind": "episode",
                                    "title": row["parsed_title"] or Path(row["episode_path"]).stem,
                                    "drive": drive,
                                    "confidence": float(row["confidence"] or 0.0),
                                    "context": {
                                        "season": row["season_number"],
                                        "episodes": _load_json_list(row["episode_numbers_json"]),
                                    },
                                }
                            )
                    except sqlite3.DatabaseError:
                        pass
            finally:
                conn.close()
        hits.sort(key=lambda item: item.get("confidence", 0.0), reverse=True)
        return hits[:top_k]
        
    def _semantic_config(self) -> SemanticConfig:
        return SemanticConfig.from_settings(self.working_dir, self._settings)

    def _resolve_catalog_path(self) -> Path:
        settings_value = self._settings.get("catalog_db")
        if isinstance(settings_value, str) and settings_value.strip():
            candidate = self._expand_user_path(settings_value.strip())
            if candidate:
                return candidate
        return get_catalog_db_path(self.working_dir)

    def _expand_user_path(self, value: str) -> Path:
        expanded = os.path.expandvars(os.path.expanduser(value))
        path = Path(expanded)
        if not path.is_absolute():
            path = self.working_dir / path
        return path.resolve()

    @contextmanager
    def _catalog(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect(self.catalog_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _shard(self, drive_label: str) -> Iterator[sqlite3.Connection]:
        shard_path = self._shard_path_for(drive_label)
        conn = self._connect(shard_path)
        conn.create_function("BASENAME", 1, _lower_basename)
        try:
            yield conn
        finally:
            conn.close()

    def _shard_path_for(self, drive_label: str) -> Path:
        if not drive_label:
            raise LookupError("unknown drive_label")
        shard_path = get_shard_db_path(self.working_dir, drive_label)
        if not shard_path.exists():
            with self._catalog() as conn:
                cur = conn.execute(
                    "SELECT 1 FROM drives WHERE label = ? LIMIT 1",
                    (drive_label,),
                )
                if cur.fetchone() is None:
                    raise LookupError("unknown drive_label")
            raise FileNotFoundError(f"shard not found for drive_label={drive_label!r}")
        return shard_path

    def _connect(self, path: Path) -> sqlite3.Connection:
        conn = connect(path, read_only=True, check_same_thread=False)
        try:
            conn.execute("PRAGMA query_only = 1")
        except sqlite3.DatabaseError:
            pass
        conn.row_factory = sqlite3.Row
        return conn

    def _structure_tables_present(self, conn: sqlite3.Connection) -> bool:
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='folder_profile'"
            )
            return cursor.fetchone() is not None
        except sqlite3.DatabaseError:
            return False

    def _doc_preview_tables_present(self, conn: sqlite3.Connection) -> bool:
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='docs_preview'"
            )
            return cursor.fetchone() is not None
        except sqlite3.DatabaseError:
            return False

    def _textlite_tables_present(self, conn: sqlite3.Connection) -> bool:
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='textlite_preview'"
            )
            return cursor.fetchone() is not None
        except sqlite3.DatabaseError:
            return False

    def _textverify_tables_present(self, conn: sqlite3.Connection) -> bool:
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='textverify_artifacts'"
            )
            return cursor.fetchone() is not None
        except sqlite3.DatabaseError:
            return False

    def _music_tables_present(self, conn: sqlite3.Connection) -> bool:
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='music_minimal'"
            )
            return cursor.fetchone() is not None
        except sqlite3.DatabaseError:
            return False

    def _music_review_table_present(self, conn: sqlite3.Connection) -> bool:
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='music_review_queue'"
            )
            return cursor.fetchone() is not None
        except sqlite3.DatabaseError:
            return False

    def resolve_pagination(self, limit: Optional[int], offset: Optional[int]) -> Pagination:
        try:
            lim = int(limit) if limit is not None else self.default_limit
        except (TypeError, ValueError):
            lim = self.default_limit
        try:
            off = int(offset or 0)
        except (TypeError, ValueError):
            off = 0
        lim = max(1, min(lim, self.max_page_size))
        off = max(0, off)
        return Pagination(limit=lim, offset=off)
    def list_drives(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        with self._catalog() as conn:
            has_binding = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drive_binding'"
                ).fetchone()
            )
            if has_binding:
                cursor = conn.execute(
                    """
                    SELECT d.label,
                           NULLIF(TRIM(COALESCE(d.drive_type, '')),'') AS drive_type,
                           NULLIF(TRIM(COALESCE(d.scanned_at, '')),'') AS scanned_at,
                           b.volume_guid,
                           b.volume_serial_hex,
                           b.filesystem,
                           b.marker_seen,
                           b.marker_last_scan_utc,
                           b.last_scan_usn,
                           b.last_scan_utc AS binding_last_scan_utc
                    FROM drives AS d
                    LEFT JOIN drive_binding AS b ON b.drive_label = d.label
                    ORDER BY d.label COLLATE NOCASE ASC
                    """
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT label,
                           NULLIF(TRIM(COALESCE(drive_type, '')),'') AS drive_type,
                           NULLIF(TRIM(COALESCE(scanned_at, '')),'') AS scanned_at
                    FROM drives
                    ORDER BY label COLLATE NOCASE ASC
                    """
                )
            for row in cursor.fetchall():
                label = row["label"]
                binding_last_scan = row["binding_last_scan_utc"] if has_binding else None
                marker_seen_raw = row["marker_seen"] if has_binding else None
                rows.append(
                    {
                        "label": label,
                        "type": row["drive_type"],
                        "last_scan_utc": binding_last_scan or row["scanned_at"],
                        "shard_path": str(get_shard_db_path(self.working_dir, label)),
                        "volume_guid": row["volume_guid"] if has_binding else None,
                        "volume_serial_hex": row["volume_serial_hex"] if has_binding else None,
                        "filesystem": row["filesystem"] if has_binding else None,
                        "marker_seen": bool(marker_seen_raw) if marker_seen_raw is not None else False,
                        "marker_last_scan_utc": row["marker_last_scan_utc"] if has_binding else None,
                        "last_scan_usn": row["last_scan_usn"] if has_binding else None,
                    }
                )
        return rows

    def doc_preview_page(
        self,
        drive_label: str,
        *,
        q: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        with self._shard(drive_label) as conn:
            if not self._doc_preview_tables_present(conn):
                return [], pagination, None, 0
            params: List[object]
            if q:
                query = (
                    """
                    SELECT dp.path, dp.doc_type, dp.lang, dp.pages_sampled, dp.chars_used,
                           dp.summary, dp.keywords, dp.updated_utc
                    FROM docs_fts AS ft
                    JOIN docs_preview AS dp ON dp.path = ft.path
                    WHERE docs_fts MATCH ?
                    ORDER BY bm25(docs_fts)
                    LIMIT ? OFFSET ?
                    """
                )
                params = [q, pagination.limit, pagination.offset]
                rows = conn.execute(query, params).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH ?", (q,)
                ).fetchone()[0]
            else:
                query = (
                    """
                    SELECT path, doc_type, lang, pages_sampled, chars_used, summary, keywords, updated_utc
                    FROM docs_preview
                    ORDER BY updated_utc DESC
                    LIMIT ? OFFSET ?
                    """
                )
                params = [pagination.limit, pagination.offset]
                rows = conn.execute(query, params).fetchall()
                total = conn.execute("SELECT COUNT(*) FROM docs_preview").fetchone()[0]
        results = [dict(row) for row in rows]
        next_offset: Optional[int] = None
        consumed = pagination.offset + len(results)
        if total is not None and consumed < total:
            next_offset = consumed
        return results, pagination, next_offset, int(total)

    def textlite_page(
        self,
        drive_label: str,
        *,
        q: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        with self._shard(drive_label) as conn:
            if not self._textlite_tables_present(conn):
                return [], pagination, None, 0
            params: List[object]
            if q:
                query = (
                    """
                    SELECT prev.path, prev.kind, prev.bytes_sampled, prev.lines_sampled,
                           prev.summary, prev.keywords, prev.schema_json, prev.updated_utc
                    FROM textlite_fts AS ft
                    JOIN textlite_preview AS prev ON prev.path = ft.path
                    WHERE textlite_fts MATCH ?
                    ORDER BY bm25(textlite_fts)
                    LIMIT ? OFFSET ?
                    """
                )
                params = [q, pagination.limit, pagination.offset]
                rows = conn.execute(query, params).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM textlite_fts WHERE textlite_fts MATCH ?", (q,)
                ).fetchone()[0]
            else:
                query = (
                    """
                    SELECT path, kind, bytes_sampled, lines_sampled, summary, keywords, schema_json, updated_utc
                    FROM textlite_preview
                    ORDER BY updated_utc DESC
                    LIMIT ? OFFSET ?
                    """
                )
                params = [pagination.limit, pagination.offset]
                rows = conn.execute(query, params).fetchall()
                total = conn.execute("SELECT COUNT(*) FROM textlite_preview").fetchone()[0]
        results = [dict(row) for row in rows]
        next_offset: Optional[int] = None
        consumed = pagination.offset + len(results)
        if total is not None and consumed < total:
            next_offset = consumed
        return results, pagination, next_offset, int(total)

    def structure_summary(self, drive_label: str) -> Dict[str, Any]:
        with self._shard(drive_label) as conn:
            if not self._structure_tables_present(conn):
                return {
                    "drive_label": drive_label,
                    "total": 0,
                    "confident": 0,
                    "medium": 0,
                    "low": 0,
                    "updated_utc": None,
                }
            total = conn.execute("SELECT COUNT(*) FROM folder_profile").fetchone()[0]
            thresholds = self._settings.get("structure") if isinstance(self._settings.get("structure"), dict) else {}
            high = float(thresholds.get("high_threshold", 0.8))
            low = float(thresholds.get("low_threshold", 0.5))
            confident = conn.execute(
                "SELECT COUNT(*) FROM folder_profile WHERE confidence >= ?",
                (high,),
            ).fetchone()[0]
            medium = conn.execute(
                "SELECT COUNT(*) FROM folder_profile WHERE confidence >= ? AND confidence < ?",
                (low, high),
            ).fetchone()[0]
            low_count = conn.execute(
                "SELECT COUNT(*) FROM folder_profile WHERE confidence < ?",
                (low,),
            ).fetchone()[0]
            updated = conn.execute(
                "SELECT MAX(updated_utc) FROM folder_profile"
            ).fetchone()[0]
        return {
            "drive_label": drive_label,
            "total": int(total or 0),
            "confident": int(confident or 0),
            "medium": int(medium or 0),
            "low": int(low_count or 0),
            "updated_utc": updated,
        }

    def structure_review_page(
        self,
        drive_label: str,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        with self._shard(drive_label) as conn:
            if not self._structure_tables_present(conn):
                return [], pagination, None, 0
            total = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
            cursor = conn.execute(
                """
                SELECT rq.folder_path,
                       rq.confidence,
                       rq.reasons_json,
                       rq.questions_json,
                       fp.kind,
                       fp.parsed_title,
                       fp.parsed_year,
                       fp.issues_json
                FROM review_queue AS rq
                LEFT JOIN folder_profile AS fp ON fp.folder_path = rq.folder_path
                ORDER BY rq.confidence ASC, rq.folder_path ASC
                LIMIT ? OFFSET ?
                """,
                (pagination.limit, pagination.offset),
            )
            rows: List[Dict[str, Any]] = []
            for entry in cursor.fetchall():
                try:
                    reasons = json.loads(entry["reasons_json"]) if entry["reasons_json"] else []
                except Exception:
                    reasons = []
                try:
                    questions = json.loads(entry["questions_json"]) if entry["questions_json"] else []
                except Exception:
                    questions = []
                try:
                    issues = json.loads(entry["issues_json"]) if entry["issues_json"] else []
                except Exception:
                    issues = []
                rows.append(
                    {
                        "folder_path": entry["folder_path"],
                        "confidence": float(entry["confidence"] or 0.0),
                        "reasons": reasons,
                        "questions": questions,
                        "kind": entry["kind"],
                        "parsed_title": entry["parsed_title"],
                        "parsed_year": entry["parsed_year"],
                        "issues": issues,
                    }
                )
        next_offset = None
        if pagination.offset + len(rows) < int(total or 0):
            next_offset = pagination.offset + len(rows)
        return rows, pagination, next_offset, int(total or 0)

    def music_page(
        self,
        drive_label: str,
        *,
        q: Optional[str] = None,
        ext: Optional[str] = None,
        min_confidence: Optional[float] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        clauses: List[str] = ["drive_label = ?"]
        params: List[Any] = [drive_label]
        if q:
            lowered = str(q).lower()
            pattern = f"%{lowered}%"
            clauses.append(
                (
                    "(LOWER(path) LIKE ? OR BASENAME(path) LIKE ? OR "
                    "LOWER(COALESCE(artist,'')) LIKE ? OR "
                    "LOWER(COALESCE(title,'')) LIKE ? OR "
                    "LOWER(COALESCE(album,'')) LIKE ?)"
                )
            )
            params.extend([pattern, pattern, pattern, pattern, pattern])
        if ext:
            clauses.append("LOWER(COALESCE(ext,'')) = ?")
            params.append(str(ext).lower())
        if min_confidence is not None:
            try:
                conf = float(min_confidence)
            except (TypeError, ValueError) as exc:
                raise ValueError("min_confidence must be a number") from exc
            conf = max(0.0, min(1.0, conf))
            clauses.append("score >= ?")
            params.append(conf)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        results: List[Dict[str, Any]] = []
        next_offset: Optional[int] = None
        total_estimate: Optional[int] = None
        with self._shard(drive_label) as conn:
            if not self._music_tables_present(conn):
                return [], pagination, None, 0
            limit_plus = pagination.limit + 1
            cursor = conn.execute(
                f"""
                SELECT path, drive_label, ext, artist, title, album, track, score,
                       score_reasons, parse_reasons, parsed_utc
                FROM music_minimal
                {where_sql}
                ORDER BY score DESC, path COLLATE NOCASE ASC
                LIMIT ? OFFSET ?
                """,
                (*params, limit_plus, pagination.offset),
            )
            fetched = cursor.fetchall()
            if len(fetched) > pagination.limit:
                fetched = fetched[: pagination.limit]
                next_offset = pagination.offset + pagination.limit
            for row in fetched:
                results.append(
                    {
                        "path": row["path"],
                        "drive_label": row["drive_label"],
                        "ext": row["ext"],
                        "artist": row["artist"],
                        "title": row["title"],
                        "album": row["album"],
                        "track_no": row["track"],
                        "confidence": float(row["score"] or 0.0),
                        "reasons": _load_json_list(row["score_reasons"]),
                        "suggestions": _load_json_list(row["parse_reasons"]),
                        "parsed_utc": row["parsed_utc"],
                    }
                )
            total_estimate = self._estimate_total(conn, "music_minimal", clauses, params)
        return results, pagination, next_offset, total_estimate

    def music_review_page(
        self,
        drive_label: str,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        results: List[Dict[str, Any]] = []
        next_offset: Optional[int] = None
        total_estimate: Optional[int] = None
        clauses = ["drive_label = ?"]
        params: List[Any] = [drive_label]
        with self._shard(drive_label) as conn:
            if not self._music_review_table_present(conn):
                return [], pagination, None, 0
            limit_plus = pagination.limit + 1
            cursor = conn.execute(
                """
                SELECT path, drive_label, ext, score, reasons_json, suggestions_json, queued_utc
                FROM music_review_queue
                WHERE drive_label = ?
                ORDER BY score ASC, queued_utc ASC, path COLLATE NOCASE ASC
                LIMIT ? OFFSET ?
                """,
                (drive_label, limit_plus, pagination.offset),
            )
            fetched = cursor.fetchall()
            if len(fetched) > pagination.limit:
                fetched = fetched[: pagination.limit]
                next_offset = pagination.offset + pagination.limit
            for row in fetched:
                results.append(
                    {
                        "path": row["path"],
                        "drive_label": row["drive_label"],
                        "ext": row["ext"],
                        "confidence": float(row["score"] or 0.0),
                        "reasons": _load_json_list(row["reasons_json"]),
                        "suggestions": _load_json_list(row["suggestions_json"]),
                        "queued_utc": row["queued_utc"],
                    }
                )
            total_estimate = self._estimate_total(
                conn, "music_review_queue", clauses, params
            )
        return results, pagination, next_offset, total_estimate

    def structure_details(
        self, drive_label: str, folder_path: str
    ) -> Optional[Dict[str, Any]]:
        with self._shard(drive_label) as conn:
            if not self._structure_tables_present(conn):
                return None
            cursor = conn.execute(
                """
                SELECT folder_path,
                       kind,
                       main_video_path,
                       parsed_title,
                       parsed_year,
                       assets_json,
                       issues_json,
                       confidence,
                       source_signals_json,
                       updated_utc
                FROM folder_profile
                WHERE folder_path = ?
                """,
                (folder_path,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            try:
                assets = json.loads(row["assets_json"]) if row["assets_json"] else {}
            except Exception:
                assets = {}
            try:
                issues = json.loads(row["issues_json"]) if row["issues_json"] else []
            except Exception:
                issues = []
            try:
                signals = (
                    json.loads(row["source_signals_json"])
                    if row["source_signals_json"]
                    else {}
                )
            except Exception:
                signals = {}
            candidates_cursor = conn.execute(
                """
                SELECT source, candidate_id, title, year, score, extra_json
                FROM folder_candidates
                WHERE folder_path = ?
                ORDER BY score DESC
                """,
                (folder_path,),
            )
            candidates: List[Dict[str, Any]] = []
            for candidate in candidates_cursor.fetchall():
                try:
                    extra = json.loads(candidate["extra_json"]) if candidate["extra_json"] else {}
                except Exception:
                    extra = {}
                candidates.append(
                    {
                        "source": candidate["source"],
                        "candidate_id": candidate["candidate_id"] or None,
                        "title": candidate["title"],
                        "year": candidate["year"],
                        "score": float(candidate["score"] or 0.0),
                        "extra": extra,
                    }
                )
        return {
            "folder_path": row["folder_path"],
            "kind": row["kind"],
            "main_video_path": row["main_video_path"],
            "parsed_title": row["parsed_title"],
            "parsed_year": row["parsed_year"],
            "assets": assets if isinstance(assets, dict) else {},
            "issues": issues if isinstance(issues, list) else [],
            "confidence": float(row["confidence"] or 0.0),
            "source_signals": signals if isinstance(signals, dict) else {},
            "updated_utc": row["updated_utc"],
            "candidates": candidates,
        }

    def inventory_page(
        self,
        drive_label: str,
        *,
        q: Optional[str] = None,
        category: Optional[str] = None,
        ext: Optional[str] = None,
        mime: Optional[str] = None,
        since: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        clauses: List[str] = []
        params: List[Any] = []
        if q:
            pattern = f"%{q.lower()}%"
            clauses.append("(LOWER(path) LIKE ? OR BASENAME(path) LIKE ?)")
            params.extend([pattern, pattern])
        if category:
            clauses.append("LOWER(COALESCE(category,'')) = ?")
            params.append(category.lower())
        if ext:
            clauses.append("LOWER(COALESCE(ext,'')) = ?")
            params.append(ext.lower())
        if mime:
            clauses.append("LOWER(COALESCE(mime,'')) LIKE ?")
            params.append(f"{mime.lower()}%")
        if since:
            normalized = _normalize_iso8601(since)
            clauses.append("mtime_utc >= ?")
            params.append(normalized)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        results: List[Dict[str, Any]] = []
        next_offset: Optional[int] = None
        total_estimate: Optional[int] = None
        with self._shard(drive_label) as conn:
            limit_plus = pagination.limit + 1
            cursor = conn.execute(
                f"""
                SELECT path, size_bytes, mtime_utc, category, drive_label, ext, mime
                FROM inventory
                {where_sql}
                ORDER BY path COLLATE NOCASE ASC
                LIMIT ? OFFSET ?
                """,
                (*params, limit_plus, pagination.offset),
            )
            fetched = cursor.fetchall()
            if len(fetched) > pagination.limit:
                fetched = fetched[: pagination.limit]
                next_offset = pagination.offset + pagination.limit
            for row in fetched:
                path_value = row["path"]
                results.append(
                    {
                        "path": path_value,
                        "name": _basename(path_value),
                        "category": row["category"],
                        "size_bytes": int(row["size_bytes"] or 0),
                        "mtime_utc": row["mtime_utc"],
                        "drive_label": row["drive_label"],
                        "ext": row["ext"],
                        "mime": row["mime"],
                    }
                )
            total_estimate = self._estimate_total(conn, "inventory", clauses, params)
        return results, pagination, next_offset, total_estimate
    def inventory_row(self, drive_label: str, path: str) -> Optional[Dict[str, Any]]:
        with self._shard(drive_label) as conn:
            cursor = conn.execute(
                """
                SELECT path, size_bytes, mtime_utc, ext, mime, category, drive_label, drive_type, indexed_utc
                FROM inventory
                WHERE path = ?
                LIMIT 1
                """,
                (path,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {key: row[key] for key in row.keys()}

    def drive_stats(self, drive_label: str) -> Dict[str, Any]:
        totals: Dict[str, int] = {}
        total_files = 0
        scanned_at: Optional[str] = None
        with self._catalog() as conn:
            cursor = conn.execute(
                """
                SELECT total_files, by_video, by_audio, by_image, by_document, by_archive,
                       by_executable, by_other, scan_ts_utc
                FROM inventory_stats
                WHERE drive_label = ?
                ORDER BY datetime(scan_ts_utc) DESC
                LIMIT 1
                """,
                (drive_label,),
            )
            row = cursor.fetchone()
            if row is not None:
                total_files = int(row["total_files"] or 0)
                totals = {
                    "video": int(row["by_video"] or 0),
                    "audio": int(row["by_audio"] or 0),
                    "image": int(row["by_image"] or 0),
                    "document": int(row["by_document"] or 0),
                    "archive": int(row["by_archive"] or 0),
                    "executable": int(row["by_executable"] or 0),
                    "other": int(row["by_other"] or 0),
                }
                scanned_at = row["scan_ts_utc"]
        if not totals:
            totals = {}
            with self._shard(drive_label) as conn:
                cursor = conn.execute(
                    """
                    SELECT COALESCE(NULLIF(TRIM(category), ''), 'other') AS cat,
                           COUNT(*) AS count
                    FROM inventory
                    GROUP BY cat
                    """
                )
                for row in cursor.fetchall():
                    category = str(row["cat"])
                totals[category] = int(row["count"] or 0)
                total_files = sum(totals.values())
        return {
            "drive_label": drive_label,
            "totals": {
                "total_files": total_files,
                "by_category": totals,
                "scanned_at_utc": scanned_at,
            },
        }

    def report_bundle(
        self,
        drive_label: str,
        *,
        top_n: int = 20,
        folder_depth: int = 2,
        recent_days: int = 30,
        largest_limit: int = 100,
    ) -> reports_util.ReportBundle:
        shard_path = self._shard_path_for(drive_label)
        catalog = self.catalog_path if self.catalog_path.exists() else None
        return reports_util.generate_report(
            shard_path,
            catalog,
            drive_label,
            top_n=top_n,
            folder_depth=folder_depth,
            recent_days=recent_days,
            largest_limit=largest_limit,
        )

    def report_overview(self, drive_label: str) -> reports_util.OverviewResult:
        shard_path = self._shard_path_for(drive_label)
        catalog = self.catalog_path if self.catalog_path.exists() else None
        return reports_util.fetch_overview(shard_path, catalog, drive_label)

    def report_top_extensions(
        self, drive_label: str, limit: int
    ) -> reports_util.TopExtensionsResult:
        shard_path = self._shard_path_for(drive_label)
        return reports_util.fetch_top_extensions(shard_path, max(1, int(limit)))

    def report_largest_files(
        self, drive_label: str, limit: int
    ) -> List[reports_util.LargestFileRow]:
        shard_path = self._shard_path_for(drive_label)
        return reports_util.fetch_largest_files(shard_path, max(1, int(limit)))

    def report_heaviest_folders(
        self, drive_label: str, depth: int, limit: int
    ) -> List[reports_util.HeaviestFolderRow]:
        shard_path = self._shard_path_for(drive_label)
        return reports_util.fetch_heaviest_folders(
            shard_path, max(0, int(depth)), max(1, int(limit))
        )

    def report_recent_changes(
        self, drive_label: str, days: int, limit: int
    ) -> reports_util.RecentChangesResult:
        shard_path = self._shard_path_for(drive_label)
        return reports_util.fetch_recent_changes(
            shard_path, max(0, int(days)), max(1, int(limit))
        )
    def features_page(
        self,
        drive_label: str,
        *,
        path_query: Optional[str] = None,
        kind: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        pagination = self.resolve_pagination(limit, offset)
        clauses: List[str] = []
        params: List[Any] = []
        if path_query:
            pattern = f"%{path_query.lower()}%"
            clauses.append("(LOWER(path) LIKE ? OR BASENAME(path) LIKE ?)")
            params.extend([pattern, pattern])
        if kind:
            clauses.append("LOWER(kind) = ?")
            params.append(kind.lower())
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        results: List[Dict[str, Any]] = []
        next_offset: Optional[int] = None
        total_estimate: Optional[int] = None
        with self._shard(drive_label) as conn:
            limit_plus = pagination.limit + 1
            cursor = conn.execute(
                f"""
                SELECT path, kind, dim, frames_used, updated_utc
                FROM features
                {where_sql}
                ORDER BY path COLLATE NOCASE ASC
                LIMIT ? OFFSET ?
                """,
                (*params, limit_plus, pagination.offset),
            )
            fetched = cursor.fetchall()
            if len(fetched) > pagination.limit:
                fetched = fetched[: pagination.limit]
                next_offset = pagination.offset + pagination.limit
            for row in fetched:
                results.append(
                    {
                        "path": row["path"],
                        "kind": row["kind"],
                        "dim": int(row["dim"] or 0),
                        "frames_used": int(row["frames_used"] or 0),
                        "updated_utc": row["updated_utc"],
                    }
                )
            total_estimate = self._estimate_total(conn, "features", clauses, params)
        return results, pagination, next_offset, total_estimate

    def feature_vector(self, drive_label: str, path: str) -> Optional[Dict[str, Any]]:
        with self._shard(drive_label) as conn:
            cursor = conn.execute(
                """
                SELECT path, kind, dim, vec
                FROM features
                WHERE path = ?
                LIMIT 1
                """,
                (path,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            dim = int(row["dim"] or 0)
            blob = row["vec"]
            vector = _blob_to_floats(blob, dim)
            return {
                "path": row["path"],
                "dim": dim,
                "kind": row["kind"],
                "vector": vector,
            }
    def _estimate_total(
        self,
        conn: sqlite3.Connection,
        table: str,
        clauses: Sequence[str],
        params: Sequence[Any],
    ) -> Optional[int]:
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        cursor = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM (
                SELECT 1
                FROM {table}
                {where_sql}
                LIMIT { _COUNT_GUARD + 1 }
            )
            """,
            tuple(params),
        )
        count = cursor.fetchone()["count"]
        if count > _COUNT_GUARD:
            return None
        return int(count)

    def semantic_search(
        self,
        query: str,
        *,
        mode: str = "ann",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        drive_label: Optional[str] = None,
        hybrid: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], int]:
        config = self._semantic_config()
        pagination = self.resolve_pagination(limit, offset)
        searcher = SemanticSearcher(config)
        results, total = searcher.search(
            query,
            limit=pagination.limit,
            offset=pagination.offset,
            drive_label=drive_label,
            mode=mode,
            hybrid=hybrid,
        )
        next_offset = (
            pagination.offset + len(results)
            if len(results) == pagination.limit
            else None
        )
        return results, pagination, next_offset, total

    def textverify_page(
        self,
        drive_label: str,
        *,
        min_score: Optional[float] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Pagination, Optional[int], Optional[int]]:
        with self._shard(drive_label) as conn:
            if not self._textverify_tables_present(conn):
                pagination = self.resolve_pagination(limit, offset)
                return [], pagination, None, None
            pagination = self.resolve_pagination(limit, offset)
            params: List[Any] = []
            sql = "SELECT * FROM textverify_artifacts"
            if min_score is not None:
                sql += " WHERE aggregated_score >= ?"
                params.append(float(min_score))
            sql += " ORDER BY updated_utc DESC LIMIT ? OFFSET ?"
            params.extend([pagination.limit, pagination.offset])
            rows = conn.execute(sql, params).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                payload = dict(row)
                payload["has_local_subs"] = bool(payload.get("has_local_subs"))
                payload["keywords"] = self._parse_keywords(payload.get("keywords"))
                results.append(payload)
            next_offset = (
                pagination.offset + pagination.limit if len(results) == pagination.limit else None
            )
            return results, pagination, next_offset, None

    def textverify_details(self, drive_label: str, path: str) -> Optional[Dict[str, Any]]:
        with self._shard(drive_label) as conn:
            if not self._textverify_tables_present(conn):
                return None
            row = conn.execute(
                "SELECT * FROM textverify_artifacts WHERE path = ?",
                (path,),
            ).fetchone()
            if not row:
                return None
            payload = dict(row)
            payload["has_local_subs"] = bool(payload.get("has_local_subs"))
            payload["keywords"] = self._parse_keywords(payload.get("keywords"))
            return payload

    def _parse_keywords(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed if str(item).strip()]
            except Exception:
                pass
            return [item.strip() for item in value.split(",") if item.strip()]
        return []

    def semantic_index(self, *, rebuild: bool = False) -> Dict[str, int]:
        config = self._semantic_config()
        indexer = SemanticIndexer(config)
        return indexer.build(rebuild=rebuild)

    def semantic_transcribe(self) -> Dict[str, int]:
        config = self._semantic_config()
        transcriber = SemanticTranscriber(config)
        return transcriber.run()

    def semantic_status(self) -> Dict[str, Any]:
        config = self._semantic_config()
        with semantic_connection(config.working_dir) as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS count FROM semantic_documents"
            ).fetchone()["count"]
            latest = conn.execute(
                "SELECT MAX(updated_utc) AS updated FROM semantic_documents"
            ).fetchone()["updated"]
        return {
            "documents": int(total or 0),
            "latest_updated_utc": latest,
            "vector_dim": config.vector_dim,
        }


def _lower_basename(path_value: Any) -> str:
    if not isinstance(path_value, str):
        return ""
    return _basename(path_value).lower()


def _basename(path_value: str) -> str:
    if not path_value:
        return ""
    normalized = path_value.replace("\\", "/")
    return normalized.rsplit("/", 1)[-1]


def _blob_to_floats(blob: Any, dim: int) -> List[float]:
    if blob is None:
        return []
    try:
        from array import array

        arr = array("f")
        arr.frombytes(blob)
        if dim and len(arr) > dim:
            arr = arr[:dim]
        return [float(v) for v in arr]
    except Exception:
        import struct

        if dim <= 0:
            dim = len(blob) // 4
        try:
            return list(struct.unpack(f"<{dim}f", blob[: dim * 4]))
        except struct.error:
            return []


def _normalize_iso8601(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "DataAccess",
    "Pagination",
]
