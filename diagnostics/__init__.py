"""Diagnostics package for VideoCatalog."""

from .preflight import run_preflight
from .smoke import run_smoke_tests
from .report import build_report_snapshot, export_report_bundle
from .logs import log_event, query_logs, purge_old_logs, latest_preflight_path, latest_smoke_path
from .tools import DiagnosticsTools

__all__ = [
    "run_preflight",
    "run_smoke_tests",
    "build_report_snapshot",
    "export_report_bundle",
    "log_event",
    "query_logs",
    "purge_old_logs",
    "latest_preflight_path",
    "latest_smoke_path",
    "DiagnosticsTools",
]
