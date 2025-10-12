"""Read-only diagnostics tools exposed to the assistant runtime."""
from __future__ import annotations

import json
from dataclasses import dataclass
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core import db as core_db
from core.paths import get_catalog_db_path, resolve_working_dir
from core.settings import load_settings

from .logs import query_logs
from .preflight import run_preflight
from .report import build_report_snapshot, get_report, list_reports
from .smoke import DEFAULT_TARGETS, run_smoke


@dataclass(slots=True)
class ToolGuard:
    working_dir: Path

    def ensure_within_workdir(self, path: Path) -> None:
        resolved = path.resolve()
        if not str(resolved).startswith(str(self.working_dir.resolve())):
            raise PermissionError("Diagnostics tools may only read within working_dir")


class DiagnosticsTools:
    """Read-only tool facade for assistant integration."""

    def __init__(self, working_dir: Optional[Path] = None) -> None:
        self.working_dir = working_dir or resolve_working_dir()
        self.settings = load_settings(self.working_dir) or {}
        self.guard = ToolGuard(self.working_dir)

    # ------------------------------------------------------------------
    def diag_run_preflight(self) -> Dict[str, Any]:
        return run_preflight(working_dir=self.working_dir, settings=self.settings)

    def diag_run_smoke(
        self,
        subsystems: Optional[Iterable[str]] = None,
        budget: Optional[int] = None,
    ) -> Dict[str, Any]:
        allowed = [name for name in (subsystems or DEFAULT_TARGETS) if name in DEFAULT_TARGETS]
        return run_smoke(allowed, budget=budget, working_dir=self.working_dir, settings=self.settings)

    def diag_get_logs(self, filters: Optional[Dict[str, Any]] = None, limit: int = 200) -> Dict[str, Any]:
        filters = filters or {}
        event_id = filters.get("event_id")
        module = filters.get("module")
        level = filters.get("level")
        return query_logs(
            working_dir=self.working_dir,
            event_id=int(event_id) if event_id is not None else None,
            module=module,
            level=level,
            limit=limit,
        )

    def diag_get_metrics(self, window_min: int = 60) -> Dict[str, Any]:
        db_path = get_catalog_db_path(self.working_dir)
        conn = core_db.connect(db_path, read_only=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT ts_utc, series, labels_json, value FROM diag_metrics ORDER BY ts_utc DESC LIMIT 500"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        metrics: Dict[str, List[float]] = {}
        for _ts, series, labels_json, value in rows:
            metrics.setdefault(series, []).append(float(value))
        aggregates = {
            series: {
                "count": len(values),
                "latest": values[0] if values else 0.0,
            }
            for series, values in metrics.items()
        }
        return {"window_min": window_min, "series": aggregates}

    def diag_sql(self, view: str, where: Optional[Dict[str, Any]] = None, limit: int = 50) -> Dict[str, Any]:
        db_path = get_catalog_db_path(self.working_dir)
        conn = core_db.connect(db_path, read_only=True, timeout=2.0)
        result_rows: List[Dict[str, Any]] = []
        try:
            if view == "diag_reports":
                rows = conn.execute(
                    "SELECT id, created_utc, summary_json FROM diag_reports ORDER BY created_utc DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                result_rows = [
                    {
                        "id": row[0],
                        "created_utc": row[1],
                        "summary": json.loads(row[2]) if row[2] else {},
                    }
                    for row in rows
                ]
            elif view == "diag_metrics":
                rows = conn.execute(
                    "SELECT ts_utc, series, labels_json, value FROM diag_metrics ORDER BY ts_utc DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                result_rows = [
                    {
                        "ts_utc": row[0],
                        "series": row[1],
                        "labels": json.loads(row[2]) if row[2] else {},
                        "value": row[3],
                    }
                    for row in rows
                ]
            else:
                raise ValueError("Unsupported diagnostics view")
        except sqlite3.Error:
            result_rows = []
        finally:
            conn.close()

        if where:
            filtered: List[Dict[str, Any]] = []
            for row in result_rows:
                include = True
                for key, value in where.items():
                    if str(row.get(key)) != str(value):
                        include = False
                        break
                if include:
                    filtered.append(row)
            result_rows = filtered
        return {"rows": result_rows[:limit], "count": min(len(result_rows), limit)}

    def diag_get_report(self, report_id: Optional[str] = None) -> Dict[str, Any]:
        if report_id:
            payload = get_report(report_id, working_dir=self.working_dir)
            if payload is None:
                raise ValueError("Report not found")
            return payload
        return build_report_snapshot(working_dir=self.working_dir)

    def diag_list_reports(self) -> List[Dict[str, Any]]:
        return list_reports(working_dir=self.working_dir)


__all__ = ["DiagnosticsTools"]
