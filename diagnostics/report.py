"""Reporting helpers for diagnostics results."""
from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import db as core_db
from core.paths import get_exports_dir, get_catalog_db_path, resolve_working_dir

from .logs import (
    EVENT_RANGES,
    latest_preflight_path,
    latest_smoke_path,
    load_snapshot,
    log_event,
    new_correlation_id,
    query_logs,
)


def _ensure_export_dir(working_dir: Path) -> Path:
    exports_dir = get_exports_dir(working_dir) / "diagnostics"
    exports_dir.mkdir(parents=True, exist_ok=True)
    return exports_dir


def _fetch_reports(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, created_utc, summary_json, items_json FROM diag_reports ORDER BY created_utc DESC"
    ).fetchall()
    results: List[Dict[str, Any]] = []
    for row in rows:
        summary = json.loads(row[2]) if row[2] else {}
        items = json.loads(row[3]) if row[3] else []
        results.append(
            {
                "id": row[0],
                "created_utc": row[1],
                "summary": summary,
                "items": items,
            }
        )
    return results


def list_reports(*, working_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    resolved = working_dir or resolve_working_dir()
    db_path = get_catalog_db_path(resolved)
    conn = core_db.connect(db_path, read_only=False, timeout=5.0)
    try:
        return _fetch_reports(conn)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def get_report(report_id: str, *, working_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    resolved = working_dir or resolve_working_dir()
    db_path = get_catalog_db_path(resolved)
    conn = core_db.connect(db_path, read_only=False, timeout=5.0)
    try:
        try:
            row = conn.execute(
                "SELECT id, created_utc, summary_json, items_json FROM diag_reports WHERE id = ?",
                (report_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return {
            "id": row[0],
            "created_utc": row[1],
            "summary": json.loads(row[2]) if row[2] else {},
            "items": json.loads(row[3]) if row[3] else [],
        }
    finally:
        conn.close()


def build_report_snapshot(
    *,
    preflight: Optional[Dict[str, Any]] = None,
    smoke: Optional[Dict[str, Any]] = None,
    working_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    resolved = working_dir or resolve_working_dir()
    preflight_payload = preflight or load_snapshot("preflight", working_dir=resolved) or {}
    smoke_payload = smoke or load_snapshot("smoke", working_dir=resolved) or {}
    summary = {
        "preflight": preflight_payload.get("summary", {}),
        "smoke": smoke_payload.get("summary", {}),
    }
    return {
        "ts": time.time(),
        "preflight": preflight_payload,
        "smoke": smoke_payload,
        "summary": summary,
    }


def export_report_bundle(
    *,
    preflight: Optional[Dict[str, Any]] = None,
    smoke: Optional[Dict[str, Any]] = None,
    logs_limit: int = 500,
    working_dir: Optional[Path] = None,
) -> Path:
    resolved = working_dir or resolve_working_dir()
    snapshot = build_report_snapshot(preflight=preflight, smoke=smoke, working_dir=resolved)
    logs_payload = query_logs(working_dir=resolved, limit=logs_limit)
    exports_dir = _ensure_export_dir(resolved)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    bundle_path = exports_dir / f"diagnostics_{timestamp}.zip"

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", json.dumps(snapshot, indent=2, ensure_ascii=False))
        archive.writestr("preflight.json", json.dumps(snapshot.get("preflight", {}), indent=2, ensure_ascii=False))
        archive.writestr("smoke.json", json.dumps(snapshot.get("smoke", {}), indent=2, ensure_ascii=False))
        archive.writestr("logs.json", json.dumps(logs_payload, indent=2, ensure_ascii=False))
        preflight_path = latest_preflight_path(resolved)
        smoke_path = latest_smoke_path(resolved)
        if preflight_path.exists():
            archive.write(preflight_path, arcname=f"snapshots/{preflight_path.name}")
        if smoke_path.exists():
            archive.write(smoke_path, arcname=f"snapshots/{smoke_path.name}")

    correlation_id = new_correlation_id()
    log_event(
        event_id=EVENT_RANGES["report"][0],
        level="INFO",
        module="diagnostics.report",
        op="export_bundle",
        working_dir=resolved,
        correlation_id=correlation_id,
        duration_ms=0.0,
        ok=True,
        details={"path": str(bundle_path)},
    )
    return bundle_path


__all__ = [
    "build_report_snapshot",
    "export_report_bundle",
    "get_report",
    "list_reports",
]
