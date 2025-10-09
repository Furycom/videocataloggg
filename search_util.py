"""Helper utilities for the Quick Search feature."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

MIN_QUERY_LENGTH = 3
EXPORT_PREFIX = "quick-search"


def sanitize_query(raw: str) -> Dict[str, str]:
    """Normalize the raw query and ensure it meets the minimum length."""
    if raw is None:
        raw = ""
    normalized = " ".join(str(raw).strip().split())
    if len(normalized) < MIN_QUERY_LENGTH:
        raise ValueError(f"Type at least {MIN_QUERY_LENGTH} characters.")
    lower = normalized.lower()
    like = f"%{lower}%"
    return {"text": normalized, "lower": lower, "like": like}


def ensure_inventory_name(conn: sqlite3.Connection, *, batch_size: int = 500) -> bool:
    """Ensure the inventory table has a lowercase basename column for search."""
    cur = conn.cursor()
    info = cur.execute("PRAGMA table_info(inventory)").fetchall()
    if not info:
        raise sqlite3.OperationalError("no such table: inventory")
    has_name = any(str(col[1]).lower() == "name" for col in info)
    if not has_name:
        cur.execute("ALTER TABLE inventory ADD COLUMN name TEXT")
        conn.commit()
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inventory_name ON inventory(name)")
    conn.commit()
    missing = cur.execute(
        "SELECT rowid, path FROM inventory WHERE name IS NULL OR name = '' LIMIT ?",
        (batch_size,),
    ).fetchall()
    while missing:
        updates = []
        for rowid, path in missing:
            base = os.path.basename(path or "")
            updates.append((base.lower(), rowid))
        cur.executemany("UPDATE inventory SET name = ? WHERE rowid = ?", updates)
        conn.commit()
        missing = cur.execute(
            "SELECT rowid, path FROM inventory WHERE name IS NULL OR name = '' LIMIT ?",
            (batch_size,),
        ).fetchall()
    return True


def build_search_query(like: str, *, use_name: bool = True) -> Tuple[str, Sequence[str]]:
    if use_name:
        sql = (
            "SELECT path, name, category, size_bytes, mtime_utc, drive_label "
            "FROM inventory "
            "WHERE name LIKE ? OR path LIKE ? COLLATE NOCASE "
            "ORDER BY mtime_utc DESC "
            "LIMIT 1000"
        )
        params = (like, like)
    else:
        sql = (
            "SELECT path, name, category, size_bytes, mtime_utc, drive_label "
            "FROM inventory "
            "WHERE path LIKE ? COLLATE NOCASE "
            "ORDER BY mtime_utc DESC "
            "LIMIT 1000"
        )
        params = (like,)
    return sql, params


def _human_size(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "0 B"
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    float_size = float(size)
    for unit in units:
        if float_size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(float_size)} {unit}"
            return f"{float_size:.1f} {unit}"
        float_size /= 1024.0
    return f"{float_size:.1f} PB"


def _format_mtime(value: Any) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    text = str(value).strip()
    if not text:
        return "—"
    try:
        as_float = float(text)
    except ValueError:
        pass
    else:
        dt = datetime.fromtimestamp(as_float, tz=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def format_results(rows: Sequence[sqlite3.Row]) -> List[Dict[str, Any]]:
    formatted: List[Dict[str, Any]] = []
    for row in rows:
        path = row["path"] if isinstance(row, sqlite3.Row) else row[0]
        name = os.path.basename(path or "")
        formatted.append(
            {
                "path": path,
                "name": name,
                "category": (row["category"] if isinstance(row, sqlite3.Row) else row[2]) or "",
                "size_bytes": int((row["size_bytes"] if isinstance(row, sqlite3.Row) else row[3]) or 0),
                "size_display": _human_size(row["size_bytes"] if isinstance(row, sqlite3.Row) else row[3]),
                "modified_display": _format_mtime(row["mtime_utc"] if isinstance(row, sqlite3.Row) else row[4]),
                "modified_raw": row["mtime_utc"] if isinstance(row, sqlite3.Row) else row[4],
                "drive": (row["drive_label"] if isinstance(row, sqlite3.Row) else row[5]) or "",
                "lower_name": (row["name"] if isinstance(row, sqlite3.Row) else row[1]) or name.lower(),
            }
        )
    return formatted


def export_results(results: Iterable[Dict[str, Any]], exports_dir: Path) -> List[Path]:
    exports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = exports_dir / f"{EXPORT_PREFIX}_{timestamp}.csv"
    jsonl_path = exports_dir / f"{EXPORT_PREFIX}_{timestamp}.jsonl"
    fieldnames = ["name", "category", "size_bytes", "size_display", "modified_display", "drive", "path"]
    rows = list(results)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return [csv_path, jsonl_path]


__all__ = [
    "sanitize_query",
    "ensure_inventory_name",
    "build_search_query",
    "format_results",
    "export_results",
]
