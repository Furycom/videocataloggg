"""Read-only SQLite helpers for the VideoCatalog local API."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from paths import (
    get_catalog_db_path,
    get_shard_db_path,
    get_shards_dir,
    load_settings,
    resolve_working_dir,
)

_DEFAULT_LIMIT = 100
_MAX_PAGE_SIZE = 500
_COUNT_GUARD = 10000


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
        uri_path = path.resolve()
        try:
            conn = sqlite3.connect(
                f"file:{uri_path.as_posix()}?mode=ro&cache=shared",
                uri=True,
                check_same_thread=False,
            )
        except sqlite3.OperationalError:
            conn = sqlite3.connect(str(uri_path), check_same_thread=False)
            try:
                conn.execute("PRAGMA query_only = 1")
            except sqlite3.DatabaseError:
                pass
        conn.row_factory = sqlite3.Row
        return conn

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
                rows.append(
                    {
                        "label": label,
                        "type": row["drive_type"],
                        "last_scan_utc": row["scanned_at"],
                        "shard_path": str(get_shard_db_path(self.working_dir, label)),
                    }
                )
        return rows

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
