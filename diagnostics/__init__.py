"""Diagnostics package for VideoCatalog."""

from .preflight import run_preflight
from .smoke import run_smoke, DEFAULT_TARGETS
from .report import build_report_snapshot, export_report_bundle, list_reports, get_report
from .logs import (
    EVENT_RANGES,
    log_event,
    query_logs,
    purge_old_logs,
    latest_preflight_path,
    latest_smoke_path,
    new_correlation_id,
)
from .tools import DiagnosticsTools

__all__ = [
    "run_preflight",
    "run_smoke",
    "DEFAULT_TARGETS",
    "build_report_snapshot",
    "export_report_bundle",
    "list_reports",
    "get_report",
    "log_event",
    "query_logs",
    "purge_old_logs",
    "latest_preflight_path",
    "latest_smoke_path",
    "new_correlation_id",
    "DiagnosticsTools",
]
