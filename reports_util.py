from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional

__all__ = [
    "ReportError",
    "MissingInventoryError",
    "ReportCancelled",
    "OverviewCategory",
    "OverviewResult",
    "TopExtensionRow",
    "TopExtensionsResult",
    "LargestFileRow",
    "HeaviestFolderRow",
    "RecentChangeRow",
    "RecentChangesResult",
    "ReportParameters",
    "ReportBundle",
    "ReportColumn",
    "ReportExportColumn",
    "SectionResult",
    "fetch_overview",
    "fetch_top_extensions",
    "fetch_largest_files",
    "fetch_heaviest_folders",
    "fetch_recent_changes",
    "generate_report",
    "bundle_to_sections",
    "export_bundle_to_csv",
    "export_bundle_to_json",
]


class ReportError(RuntimeError):
    """Base error for report failures."""


class MissingInventoryError(ReportError):
    """Raised when the shard inventory is missing."""


class ReportCancelled(ReportError):
    """Raised when a background request is cancelled."""


@dataclass(slots=True)
class OverviewCategory:
    category: str
    files: int
    bytes: int


@dataclass(slots=True)
class OverviewResult:
    total_files: int
    total_size: int
    average_size: int
    categories: List[OverviewCategory]
    source: str


@dataclass(slots=True)
class TopExtensionRow:
    extension: str
    files: int
    bytes: int
    rank_count: Optional[int]
    rank_size: Optional[int]


@dataclass(slots=True)
class TopExtensionsResult:
    entries: List[TopExtensionRow]


@dataclass(slots=True)
class LargestFileRow:
    path: str
    size_bytes: int
    mtime_utc: Optional[str]
    category: Optional[str]


@dataclass(slots=True)
class HeaviestFolderRow:
    folder: str
    files: int
    bytes: int


@dataclass(slots=True)
class RecentChangeRow:
    path: str
    size_bytes: int
    mtime_utc: Optional[str]
    category: Optional[str]


@dataclass(slots=True)
class RecentChangesResult:
    total: int
    rows: List[RecentChangeRow]


@dataclass(slots=True)
class ReportParameters:
    top_n: int
    folder_depth: int
    recent_days: int
    largest_limit: int


@dataclass(slots=True)
class ReportBundle:
    drive_label: str
    overview: OverviewResult
    top_extensions: TopExtensionsResult
    largest_files: List[LargestFileRow]
    heaviest_folders: List[HeaviestFolderRow]
    recent_changes: RecentChangesResult
    params: ReportParameters
    elapsed_ms: int

    @property
    def total_rows(self) -> int:
        return (
            1
            + len(self.overview.categories)
            + len(self.top_extensions.entries)
            + len(self.largest_files)
            + len(self.heaviest_folders)
            + len(self.recent_changes.rows)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "drive_label": self.drive_label,
            "elapsed_ms": self.elapsed_ms,
            "params": asdict(self.params),
            "overview": {
                "total_files": self.overview.total_files,
                "total_size": self.overview.total_size,
                "average_size": self.overview.average_size,
                "source": self.overview.source,
                "categories": [asdict(cat) for cat in self.overview.categories],
            },
            "top_extensions": {
                "entries": [asdict(entry) for entry in self.top_extensions.entries]
            },
            "largest_files": [asdict(row) for row in self.largest_files],
            "heaviest_folders": [asdict(row) for row in self.heaviest_folders],
            "recent_changes": {
                "total": self.recent_changes.total,
                "rows": [asdict(row) for row in self.recent_changes.rows],
            },
        }


@dataclass(slots=True)
class ReportColumn:
    key: str
    heading: str
    anchor: str = "w"
    width: Optional[int] = None
    stretch: bool = True
    numeric: bool = False


@dataclass(slots=True)
class ReportExportColumn:
    key: str
    heading: str


@dataclass(slots=True)
class SectionResult:
    name: str
    columns: List[ReportColumn]
    rows: List[Dict[str, Any]]
    export_rows: List[Dict[str, Any]]
    export_columns: List[ReportExportColumn]


def generate_report(
    shard_path: Path,
    catalog_path: Optional[Path],
    drive_label: str,
    *,
    top_n: int = 20,
    folder_depth: int = 2,
    recent_days: int = 30,
    largest_limit: int = 100,
    cancel_event: Optional[threading.Event] = None,
) -> ReportBundle:
    shard = Path(shard_path)
    if not shard.exists():
        raise MissingInventoryError("inventory shard is missing")
    start = time.perf_counter()
    with _connect_readonly(shard) as conn:
        if not _inventory_exists(conn):
            raise MissingInventoryError("inventory table not found")
        _apply_pragmas(conn)
        overview = _fetch_overview(conn, catalog_path, drive_label)
        _check_cancel(cancel_event)
        top_extensions = _fetch_top_extensions(conn, top_n)
        _check_cancel(cancel_event)
        largest_files = _fetch_largest_files(conn, max(1, int(largest_limit)))
        _check_cancel(cancel_event)
        heaviest = _fetch_heaviest_folders(conn, folder_depth, top_n)
        _check_cancel(cancel_event)
        recent = _fetch_recent_changes(conn, recent_days, top_n)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    params = ReportParameters(
        top_n=max(1, int(top_n)),
        folder_depth=max(0, int(folder_depth)),
        recent_days=max(0, int(recent_days)),
        largest_limit=max(1, int(largest_limit)),
    )
    return ReportBundle(
        drive_label=drive_label,
        overview=overview,
        top_extensions=top_extensions,
        largest_files=largest_files,
        heaviest_folders=heaviest,
        recent_changes=recent,
        params=params,
        elapsed_ms=elapsed_ms,
    )


def fetch_overview(
    shard_path: Path, catalog_path: Optional[Path], drive_label: str
) -> OverviewResult:
    shard = Path(shard_path)
    if not shard.exists():
        raise MissingInventoryError("inventory shard is missing")
    with _connect_readonly(shard) as conn:
        if not _inventory_exists(conn):
            raise MissingInventoryError("inventory table not found")
        _apply_pragmas(conn)
        return _fetch_overview(conn, catalog_path, drive_label)


def fetch_top_extensions(shard_path: Path, limit: int) -> TopExtensionsResult:
    shard = Path(shard_path)
    if not shard.exists():
        raise MissingInventoryError("inventory shard is missing")
    with _connect_readonly(shard) as conn:
        if not _inventory_exists(conn):
            raise MissingInventoryError("inventory table not found")
        _apply_pragmas(conn)
        return _fetch_top_extensions(conn, limit)


def fetch_largest_files(shard_path: Path, limit: int) -> List[LargestFileRow]:
    shard = Path(shard_path)
    if not shard.exists():
        raise MissingInventoryError("inventory shard is missing")
    with _connect_readonly(shard) as conn:
        if not _inventory_exists(conn):
            raise MissingInventoryError("inventory table not found")
        _apply_pragmas(conn)
        return _fetch_largest_files(conn, limit)


def fetch_heaviest_folders(
    shard_path: Path, folder_depth: int, limit: int
) -> List[HeaviestFolderRow]:
    shard = Path(shard_path)
    if not shard.exists():
        raise MissingInventoryError("inventory shard is missing")
    with _connect_readonly(shard) as conn:
        if not _inventory_exists(conn):
            raise MissingInventoryError("inventory table not found")
        _apply_pragmas(conn)
        return _fetch_heaviest_folders(conn, folder_depth, limit)


def fetch_recent_changes(
    shard_path: Path, recent_days: int, limit: int
) -> RecentChangesResult:
    shard = Path(shard_path)
    if not shard.exists():
        raise MissingInventoryError("inventory shard is missing")
    with _connect_readonly(shard) as conn:
        if not _inventory_exists(conn):
            raise MissingInventoryError("inventory table not found")
        _apply_pragmas(conn)
        return _fetch_recent_changes(conn, recent_days, limit)


def bundle_to_sections(bundle: ReportBundle) -> Dict[str, SectionResult]:
    sections: Dict[str, SectionResult] = {}

    overview_rows: List[Dict[str, Any]] = []
    overview_export_rows: List[Dict[str, Any]] = []
    total_row = {
        "label": "Totals",
        "files": f"{bundle.overview.total_files:,}",
        "size": _format_bytes(bundle.overview.total_size),
        "average": _format_bytes(bundle.overview.average_size),
    }
    overview_rows.append(total_row)
    overview_export_rows.append(
        {
            "label": "Totals",
            "files": bundle.overview.total_files,
            "size": bundle.overview.total_size,
            "average": bundle.overview.average_size,
            "size_bytes": bundle.overview.total_size,
            "average_size_bytes": bundle.overview.average_size,
        }
    )
    for category in bundle.overview.categories:
        label = category.category.title() if category.category else "Other"
        overview_rows.append(
            {
                "label": label,
                "files": f"{category.files:,}",
                "size": _format_bytes(category.bytes),
                "average": "",
            }
        )
        overview_export_rows.append(
            {
                "label": label,
                "files": category.files,
                "size": category.bytes,
                "average": 0,
                "size_bytes": category.bytes,
                "average_size_bytes": 0,
            }
        )
    sections["overview"] = SectionResult(
        name="Overview",
        columns=[
            ReportColumn("label", "Label", anchor="w", width=220, stretch=True),
            ReportColumn("files", "Files", anchor="e", width=120, numeric=True, stretch=False),
            ReportColumn("size", "Size", anchor="e", width=140, stretch=False, numeric=True),
            ReportColumn("average", "Average size", anchor="e", width=140, stretch=False, numeric=True),
        ],
        rows=overview_rows,
        export_rows=overview_export_rows,
        export_columns=[
            ReportExportColumn("label", "Label"),
            ReportExportColumn("files", "Files"),
            ReportExportColumn("size_bytes", "Total bytes"),
            ReportExportColumn("average_size_bytes", "Average bytes"),
        ],
    )

    extension_rows: List[Dict[str, Any]] = []
    extension_raw: List[Dict[str, Any]] = []
    for entry in bundle.top_extensions.entries:
        label = entry.extension or "(none)"
        extension_rows.append(
            {
                "extension": label,
                "files": f"{entry.files:,}",
                "size": _format_bytes(entry.bytes),
                "rank_count": entry.rank_count or "—",
                "rank_size": entry.rank_size or "—",
            }
        )
        extension_raw.append(
            {
                "extension": label,
                "files": entry.files,
                "size": entry.bytes,
                "bytes": entry.bytes,
                "rank_count": entry.rank_count,
                "rank_size": entry.rank_size,
            }
        )
    sections["top_extensions"] = SectionResult(
        name="Top extensions",
        columns=[
            ReportColumn("extension", "Extension", anchor="w", width=140),
            ReportColumn("files", "Files", anchor="e", width=120, numeric=True, stretch=False),
            ReportColumn("size", "Total size", anchor="e", width=140, stretch=False, numeric=True),
            ReportColumn("rank_count", "Rank (count)", anchor="e", width=120, stretch=False, numeric=True),
            ReportColumn("rank_size", "Rank (size)", anchor="e", width=120, stretch=False, numeric=True),
        ],
        rows=extension_rows,
        export_rows=[
            {
                "extension": raw["extension"],
                "files": raw["files"],
                "bytes": raw["bytes"],
                "rank_count": raw["rank_count"],
                "rank_size": raw["rank_size"],
            }
            for raw in extension_raw
        ],
        export_columns=[
            ReportExportColumn("extension", "Extension"),
            ReportExportColumn("files", "Files"),
            ReportExportColumn("bytes", "Total bytes"),
            ReportExportColumn("rank_count", "Rank by count"),
            ReportExportColumn("rank_size", "Rank by size"),
        ],
    )

    largest_rows: List[Dict[str, Any]] = []
    largest_raw: List[Dict[str, Any]] = []
    for row in bundle.largest_files:
        largest_rows.append(
            {
                "path": row.path,
                "size": _format_bytes(row.size_bytes),
                "modified": _format_local_time(row.mtime_utc),
                "category": row.category or "",
            }
        )
        largest_raw.append(
            {
                "path": row.path,
                "size": row.size_bytes,
                "size_bytes": row.size_bytes,
                "modified": row.mtime_utc,
                "mtime_utc": row.mtime_utc,
                "category": row.category,
            }
        )
    sections["largest_files"] = SectionResult(
        name="Largest files",
        columns=[
            ReportColumn("path", "Path", anchor="w", width=480),
            ReportColumn("size", "Size", anchor="e", width=120, stretch=False, numeric=True),
            ReportColumn("modified", "Modified (local)", anchor="e", width=180, stretch=False),
            ReportColumn("category", "Category", anchor="w", width=120, stretch=False),
        ],
        rows=largest_rows,
        export_rows=largest_raw,
        export_columns=[
            ReportExportColumn("path", "Path"),
            ReportExportColumn("size_bytes", "Size bytes"),
            ReportExportColumn("mtime_utc", "Modified UTC"),
            ReportExportColumn("category", "Category"),
        ],
    )

    folder_rows: List[Dict[str, Any]] = []
    folder_raw: List[Dict[str, Any]] = []
    for row in bundle.heaviest_folders:
        folder_rows.append(
            {
                "folder": row.folder,
                "files": f"{row.files:,}",
                "size": _format_bytes(row.bytes),
            }
        )
        folder_raw.append(
            {
                "folder": row.folder,
                "files": row.files,
                "size": row.bytes,
                "bytes": row.bytes,
            }
        )
    sections["heaviest_folders"] = SectionResult(
        name="Heaviest folders",
        columns=[
            ReportColumn("folder", "Folder", anchor="w", width=420),
            ReportColumn("files", "Files", anchor="e", width=120, stretch=False, numeric=True),
            ReportColumn("size", "Total size", anchor="e", width=140, stretch=False, numeric=True),
        ],
        rows=folder_rows,
        export_rows=folder_raw,
        export_columns=[
            ReportExportColumn("folder", "Folder"),
            ReportExportColumn("files", "Files"),
            ReportExportColumn("bytes", "Total bytes"),
        ],
    )

    recent_rows: List[Dict[str, Any]] = []
    recent_raw: List[Dict[str, Any]] = []
    for row in bundle.recent_changes.rows:
        recent_rows.append(
            {
                "path": row.path,
                "size": _format_bytes(row.size_bytes),
                "modified": _format_local_time(row.mtime_utc),
                "category": row.category or "",
            }
        )
        recent_raw.append(
            {
                "path": row.path,
                "size": row.size_bytes,
                "size_bytes": row.size_bytes,
                "modified": row.mtime_utc,
                "mtime_utc": row.mtime_utc,
                "category": row.category,
            }
        )
    sections["recent_changes"] = SectionResult(
        name="Recent changes",
        columns=[
            ReportColumn("path", "Path", anchor="w", width=480),
            ReportColumn("size", "Size", anchor="e", width=120, stretch=False, numeric=True),
            ReportColumn("modified", "Modified (local)", anchor="e", width=180, stretch=False),
            ReportColumn("category", "Category", anchor="w", width=120, stretch=False),
        ],
        rows=recent_rows,
        export_rows=recent_raw,
        export_columns=[
            ReportExportColumn("path", "Path"),
            ReportExportColumn("size_bytes", "Size bytes"),
            ReportExportColumn("mtime_utc", "Modified UTC"),
            ReportExportColumn("category", "Category"),
        ],
    )

    return sections


def export_bundle_to_csv(
    bundle: ReportBundle, output_dir: Path, *, prefix: str = "reports"
) -> List[Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sections = bundle_to_sections(bundle)
    created: List[Path] = []
    for key, section in sections.items():
        safe_key = _safe_filename(key)
        path = output_dir / f"{prefix}_{safe_key}_{timestamp}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[col.key for col in section.export_columns],
            )
            writer.writeheader()
            for row in section.export_rows:
                payload = {col.key: row.get(col.key, "") for col in section.export_columns}
                writer.writerow(payload)
        created.append(path)
    return created


def export_bundle_to_json(
    bundle: ReportBundle, output_dir: Path, *, prefix: str = "reports"
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"{prefix}_{timestamp}.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(bundle.to_dict(), handle, ensure_ascii=False, indent=2)
    return path


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve()
    try:
        conn = sqlite3.connect(
            f"file:{uri.as_posix()}?mode=ro&cache=shared",
            uri=True,
            check_same_thread=False,
        )
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(uri), check_same_thread=False)
        try:
            conn.execute("PRAGMA query_only = 1")
        except sqlite3.DatabaseError:
            pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -16000")


def _inventory_exists(conn: sqlite3.Connection) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='inventory'"
    )
    return cursor.fetchone() is not None


def _fetch_overview(
    conn: sqlite3.Connection,
    catalog_path: Optional[Path],
    drive_label: str,
) -> OverviewResult:
    total_row = conn.execute(
        "SELECT COUNT(*) AS total_files, COALESCE(SUM(size_bytes), 0) AS total_size FROM inventory"
    ).fetchone()
    total_files = int(total_row["total_files"] or 0)
    total_size = int(total_row["total_size"] or 0)
    average_size = int(total_size / total_files) if total_files else 0

    categories: List[OverviewCategory] = []
    source = "inventory"
    if catalog_path is not None and catalog_path.exists():
        try:
            with _connect_readonly(catalog_path) as cat_conn:
                cursor = cat_conn.execute(
                    """
                    SELECT total_files, by_video, by_audio, by_image, by_document,
                           by_archive, by_executable, by_other
                    FROM inventory_stats
                    WHERE drive_label = ?
                    ORDER BY datetime(scan_ts_utc) DESC
                    LIMIT 1
                    """,
                    (drive_label,),
                )
                row = cursor.fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row is not None:
            stats_total = int(row["total_files"] or 0)
            if stats_total:
                source = "inventory_stats"
                categories = [
                    OverviewCategory("video", int(row["by_video"] or 0), 0),
                    OverviewCategory("audio", int(row["by_audio"] or 0), 0),
                    OverviewCategory("image", int(row["by_image"] or 0), 0),
                    OverviewCategory("document", int(row["by_document"] or 0), 0),
                    OverviewCategory("archive", int(row["by_archive"] or 0), 0),
                    OverviewCategory("executable", int(row["by_executable"] or 0), 0),
                    OverviewCategory("other", int(row["by_other"] or 0), 0),
                ]
    if not categories:
        source = "inventory"
        cursor = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(category), ''), 'other') AS category,
                   COUNT(*) AS files,
                   COALESCE(SUM(size_bytes), 0) AS bytes
            FROM inventory
            GROUP BY category
            ORDER BY files DESC
            """
        )
        for row in cursor.fetchall():
            categories.append(
                OverviewCategory(
                    str(row["category"] or "other"),
                    int(row["files"] or 0),
                    int(row["bytes"] or 0),
                )
            )
    else:
        # When stats provided counts, compute bytes per category on demand.
        by_category = {cat.category: cat for cat in categories}
        cursor = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(category), ''), 'other') AS category,
                   COALESCE(SUM(size_bytes), 0) AS bytes
            FROM inventory
            GROUP BY category
            """
        )
        for row in cursor.fetchall():
            category = str(row["category"] or "other")
            bytes_value = int(row["bytes"] or 0)
            if category in by_category:
                by_category[category].bytes = bytes_value
            else:
                by_category[category] = OverviewCategory(category, 0, bytes_value)
        categories = list(by_category.values())
        categories.sort(key=lambda item: item.files, reverse=True)

    return OverviewResult(
        total_files=total_files,
        total_size=total_size,
        average_size=average_size,
        categories=categories,
        source=source,
    )


def _normalize_extension(value: Any) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if not text.startswith(".") and len(text) <= 6:
        return f".{text}"
    return text


def _fetch_top_extensions(conn: sqlite3.Connection, limit: int) -> TopExtensionsResult:
    limit = max(1, int(limit))
    by_count = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(ext), ''), '') AS ext,
               COUNT(*) AS files,
               COALESCE(SUM(size_bytes), 0) AS bytes
        FROM inventory
        GROUP BY ext
        ORDER BY files DESC, ext ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    by_size = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(ext), ''), '') AS ext,
               COUNT(*) AS files,
               COALESCE(SUM(size_bytes), 0) AS bytes
        FROM inventory
        GROUP BY ext
        ORDER BY bytes DESC, ext ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    mapping: Dict[str, TopExtensionRow] = {}
    for idx, row in enumerate(by_count, start=1):
        ext = _normalize_extension(row["ext"])
        mapping[ext] = TopExtensionRow(
            extension=ext,
            files=int(row["files"] or 0),
            bytes=int(row["bytes"] or 0),
            rank_count=idx,
            rank_size=None,
        )
    for idx, row in enumerate(by_size, start=1):
        ext = _normalize_extension(row["ext"])
        entry = mapping.get(ext)
        if entry is None:
            entry = TopExtensionRow(
                extension=ext,
                files=int(row["files"] or 0),
                bytes=int(row["bytes"] or 0),
                rank_count=None,
                rank_size=idx,
            )
            mapping[ext] = entry
        else:
            entry.rank_size = idx
            entry.bytes = int(row["bytes"] or entry.bytes)
    ordered = sorted(
        mapping.values(),
        key=lambda item: (
            item.rank_count if item.rank_count is not None else 1_000_000,
            item.rank_size if item.rank_size is not None else 1_000_000,
            item.extension,
        ),
    )
    return TopExtensionsResult(entries=ordered)


def _fetch_largest_files(conn: sqlite3.Connection, limit: int) -> List[LargestFileRow]:
    cursor = conn.execute(
        """
        SELECT path, size_bytes, mtime_utc, category
        FROM inventory
        ORDER BY size_bytes DESC, path ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    )
    rows: List[LargestFileRow] = []
    for row in cursor.fetchall():
        rows.append(
            LargestFileRow(
                path=row["path"],
                size_bytes=int(row["size_bytes"] or 0),
                mtime_utc=row["mtime_utc"],
                category=row["category"],
            )
        )
    return rows


def _fetch_heaviest_folders(
    conn: sqlite3.Connection, folder_depth: int, limit: int
) -> List[HeaviestFolderRow]:
    depth = max(0, int(folder_depth))
    limit = max(1, int(limit))
    conn.create_function("PARENT_FOLDER", 2, _parent_folder)
    cursor = conn.execute(
        """
        SELECT parent AS folder, COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes
        FROM (
            SELECT PARENT_FOLDER(path, ?) AS parent, size_bytes
            FROM inventory
        )
        WHERE parent IS NOT NULL AND parent <> ''
        GROUP BY parent
        ORDER BY bytes DESC, parent ASC
        LIMIT ?
        """,
        (depth, limit),
    )
    rows: List[HeaviestFolderRow] = []
    for row in cursor.fetchall():
        rows.append(
            HeaviestFolderRow(
                folder=row["folder"],
                files=int(row["files"] or 0),
                bytes=int(row["bytes"] or 0),
            )
        )
    return rows


def _fetch_recent_changes(
    conn: sqlite3.Connection, recent_days: int, limit: int
) -> RecentChangesResult:
    days = max(0, int(recent_days))
    limit = max(1, int(limit))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    total_row = conn.execute(
        "SELECT COUNT(*) AS total FROM inventory WHERE datetime(mtime_utc) >= datetime(?)",
        (cutoff_iso,),
    ).fetchone()
    total = int(total_row["total"] or 0) if total_row else 0
    cursor = conn.execute(
        """
        SELECT path, size_bytes, mtime_utc, category
        FROM inventory
        WHERE datetime(mtime_utc) >= datetime(?)
        ORDER BY datetime(mtime_utc) DESC
        LIMIT ?
        """,
        (cutoff_iso, limit),
    )
    rows: List[RecentChangeRow] = []
    for row in cursor.fetchall():
        rows.append(
            RecentChangeRow(
                path=row["path"],
                size_bytes=int(row["size_bytes"] or 0),
                mtime_utc=row["mtime_utc"],
                category=row["category"],
            )
        )
    return RecentChangesResult(total=total, rows=rows)


def _format_bytes(value: int) -> str:
    if value <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size):,} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} EB"


def _format_local_time(value: Optional[str]) -> str:
    if value in (None, ""):
        return "—"
    text = str(value)
    candidate = text.strip()
    if not candidate:
        return "—"
    try:
        if candidate.isdigit():
            dt = datetime.fromtimestamp(float(candidate), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return candidate


def _parent_folder(path: Optional[str], depth: int) -> str:
    if not path:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    depth = max(0, int(depth))
    pure = _pure_path(text)
    parent = pure.parent
    if str(parent) in {"", "."}:
        return ""
    if depth <= 0:
        return str(parent)
    parts = list(parent.parts)
    if isinstance(parent, PureWindowsPath):
        if parts and parts[0].startswith("\\\\"):
            root_parts = [parts[0]]
            start = 1
        else:
            root_parts = parts[:1]
            start = 1
    else:
        if parts and parts[0] == "/":
            root_parts = ["/"]
            start = 1
        else:
            root_parts = []
            start = 0
    take = min(len(parts) - start, depth)
    selected = root_parts + parts[start : start + take]
    if not selected:
        return str(parent)
    return str(parent.__class__(*selected))


def _pure_path(value: str):
    if "\\" in value or (len(value) > 1 and value[1] == ":"):
        try:
            return PureWindowsPath(value)
        except Exception:
            return PurePosixPath(value.replace("\\", "/"))
    return PurePosixPath(value)


def _safe_filename(value: str) -> str:
    cleaned = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.lower()]
    text = "".join(cleaned).strip("-")
    return text or "report"


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ReportCancelled()
