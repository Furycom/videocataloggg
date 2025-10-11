"""Aggregate diagnostics reports and export bundles."""
from __future__ import annotations

import json
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from core.paths import get_exports_dir, resolve_working_dir

from .logs import (
    EVENT_RANGES,
    latest_preflight_path,
    latest_smoke_path,
    load_snapshot,
    log_event,
    query_logs,
)


def _ensure_export_dir(working_dir: Path) -> Path:
    exports_dir = get_exports_dir(working_dir) / "diagnostics"
    exports_dir.mkdir(parents=True, exist_ok=True)
    return exports_dir


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
        "summary": summary,
        "preflight": preflight_payload,
        "smoke": smoke_payload,
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
    logs_payload = query_logs(limit=logs_limit, working_dir=resolved)

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

    log_event(
        event_id=EVENT_RANGES["smoke"][0] + 500,
        level="INFO",
        module="diagnostics.report",
        op="export_report",
        ok=True,
        working_dir=resolved,
        extra={"path": str(bundle_path)},
    )

    return bundle_path


__all__ = ["build_report_snapshot", "export_report_bundle"]
