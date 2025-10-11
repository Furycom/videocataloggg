"""Audit pack orchestration helpers."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import sqlite3

from core.db import connect

from .baseline import (
    AuditDelta,
    BaselineRecord,
    compare_to_latest,
    create_baseline,
    ensure_table,
    get_latest_baseline,
)
from .exports import AuditExportResult, export_audit_data
from .summary import AuditSummary, gather_summary

LOGGER = logging.getLogger("videocatalog.audit.run")

ProgressCallback = Callable[[str, dict], None]
HeartbeatCallback = Callable[[str], None]


class AuditCancelledError(RuntimeError):
    """Raised when an audit run is cancelled by the caller."""


@dataclass(slots=True)
class AuditRequest:
    export: bool = False
    create_baseline: bool = False
    compare_delta: bool = False
    gentle_sleep: float = 0.02


@dataclass(slots=True)
class AuditResult:
    summary: AuditSummary
    export: Optional[AuditExportResult] = None
    created_baseline: Optional[BaselineRecord] = None
    latest_baseline: Optional[BaselineRecord] = None
    delta: Optional[AuditDelta] = None


def _timestamped_dir(base: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = base / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_audit_pack(
    db_path: Path | str,
    working_dir: Path,
    request: AuditRequest,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    heartbeat_cb: Optional[HeartbeatCallback] = None,
    cancellation: Optional["CancellationToken"] = None,
) -> AuditResult:
    """Run the audit workflow using the existing catalog database."""

    from robust import CancellationToken  # local import to avoid cycles

    if cancellation is not None and not isinstance(cancellation, CancellationToken):
        raise TypeError("cancellation must be a CancellationToken or None")

    def _emit(stage: str, payload: Optional[dict] = None) -> None:
        if progress_cb:
            progress_cb(stage, payload or {})

    def _heartbeat(message: str) -> None:
        if heartbeat_cb:
            heartbeat_cb(message)

    def _check_cancel() -> None:
        if cancellation is not None and cancellation.is_set():
            raise AuditCancelledError("Audit run cancelled")

    LOGGER.info("Starting audit run: export=%s baseline=%s delta=%s", int(request.export), int(request.create_baseline), int(request.compare_delta))
    conn = connect(db_path, timeout=60.0, check_same_thread=False)
    conn.row_factory = None
    ensure_table(conn)
    try:
        _check_cancel()
        _emit("summary", {"status": "starting"})
        summary = gather_summary(
            conn,
            working_dir=working_dir,
            gentle_sleep=request.gentle_sleep,
        )
        _emit("summary", {"status": "done", "generated_utc": summary.generated_utc})
        _heartbeat("summary")

        latest_before = get_latest_baseline(conn)
        delta_result: Optional[AuditDelta] = None
        baseline_used: Optional[BaselineRecord] = latest_before
        if request.compare_delta and latest_before is not None:
            _check_cancel()
            _emit("delta", {"status": "starting", "baseline": latest_before.created_utc})
            baseline_used, delta_result = compare_to_latest(
                conn,
                summary=summary,
                gentle_sleep=request.gentle_sleep,
            )
            _emit(
                "delta",
                {
                    "status": "done",
                    "baseline": baseline_used.created_utc if baseline_used else None,
                    "changed": list(delta_result.changed_hashes) if delta_result else [],
                },
            )
            _heartbeat("delta")

        export_result: Optional[AuditExportResult] = None
        if request.export:
            _check_cancel()
            _emit("export", {"status": "starting"})
            export_result = export_audit_data(
                conn,
                working_dir=working_dir,
                summary=summary,
                gentle_sleep=request.gentle_sleep,
            )
            _emit(
                "export",
                {"status": "done", "directory": str(export_result.directory), "files": [str(p) for p in export_result.files]},
            )
            _heartbeat("export")

        baseline_created: Optional[BaselineRecord] = None
        if request.create_baseline:
            _check_cancel()
            _emit("baseline", {"status": "starting"})
            export_dir = export_result.directory if export_result else _timestamped_dir(Path(working_dir, "exports", "audit"))
            if export_result is None:
                export_result = AuditExportResult(directory=export_dir)
            baseline_created = create_baseline(
                conn,
                summary=summary,
                export_dir=export_dir,
                gentle_sleep=request.gentle_sleep,
            )
            if baseline_created.file_path:
                export_result.add(baseline_created.file_path)
            conn.commit()
            _emit(
                "baseline",
                {
                    "status": "done",
                    "created_utc": baseline_created.created_utc,
                    "path": str(baseline_created.file_path) if baseline_created.file_path else None,
                },
            )
            _heartbeat("baseline")
            baseline_used = baseline_created
        elif latest_before is not None and baseline_used is None:
            baseline_used = latest_before

        result = AuditResult(
            summary=summary,
            export=export_result,
            created_baseline=baseline_created,
            latest_baseline=baseline_used,
            delta=delta_result,
        )
        LOGGER.info("Audit run finished")
        return result
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
