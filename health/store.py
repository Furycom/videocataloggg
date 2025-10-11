from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from core import db as core_db
from core.paths import get_catalog_db_path, resolve_working_dir

from .checks import HealthItem, HealthReport

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS health_report (
  ts_utc TEXT PRIMARY KEY,
  summary_json TEXT NOT NULL,
  items_json TEXT NOT NULL
);
"""


class HealthStore:
    """Persist and retrieve health reports inside the catalog database."""

    def __init__(self, working_dir: Path | None = None) -> None:
        self._working_dir = working_dir or resolve_working_dir()
        self._db_path = get_catalog_db_path(self._working_dir)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = core_db.connect(self._db_path, read_only=False)
        try:
            conn.execute(_TABLE_SQL)
            conn.commit()
        finally:
            conn.close()

    def save(self, report: HealthReport) -> None:
        conn = core_db.connect(self._db_path, read_only=False)
        try:
            summary = json.dumps(
                {"major": report.summary.major, "minor": report.summary.minor}
            )
            items_json = json.dumps([_item_to_json(item) for item in report.items])
            conn.execute(
                "REPLACE INTO health_report (ts_utc, summary_json, items_json) VALUES (?, ?, ?)",
                (
                    _format_timestamp(report.ts),
                    summary,
                    items_json,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def latest(self) -> HealthReport | None:
        conn = core_db.connect(self._db_path, read_only=True)
        try:
            row = conn.execute(
                "SELECT ts_utc, summary_json, items_json FROM health_report ORDER BY ts_utc DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            ts_str, summary_json, items_json = row
            items = [_item_from_json(payload) for payload in json.loads(items_json)]
            return HealthReport(ts=_parse_timestamp(ts_str), items=items)
        finally:
            conn.close()


def _item_to_json(item: HealthItem) -> dict:
    payload = asdict(item)
    payload["severity"] = item.severity.value
    return payload


def _item_from_json(payload: dict) -> HealthItem:
    from .checks import HealthSeverity

    return HealthItem(
        severity=HealthSeverity(payload.get("severity", "MINOR")),
        code=payload.get("code", "UNKNOWN"),
        where=payload.get("where", ""),
        hint=payload.get("hint", ""),
        details=payload.get("details"),
    )


def _format_timestamp(ts: float) -> str:
    return "%.3f" % ts


def _parse_timestamp(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
