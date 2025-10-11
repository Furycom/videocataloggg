"""Read-only diagnostics tools exposed to the assistant runtime."""
from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.paths import get_catalog_db_path, resolve_working_dir
from core.settings import load_settings

from .logs import load_snapshot, query_logs
from .preflight import run_preflight
from .report import build_report_snapshot
from .smoke import DEFAULT_SUBSYSTEMS, run_smoke_tests


class DiagnosticsTools:
    """Convenience wrapper to expose diagnostics operations to LLM tools."""

    def __init__(self, working_dir: Optional[Path] = None) -> None:
        self.working_dir = working_dir or resolve_working_dir()
        self.settings = load_settings(self.working_dir) or {}
        self.db_path = get_catalog_db_path(self.working_dir)

    # ------------------------------------------------------------------
    def diag_run_preflight(self) -> Dict[str, Any]:
        return run_preflight(working_dir=self.working_dir)

    def diag_run_smoke(
        self,
        subsystems: Optional[Iterable[str]] = None,
        budget: Optional[int] = None,
    ) -> Dict[str, Any]:
        allowed = [name for name in (subsystems or DEFAULT_SUBSYSTEMS) if name in DEFAULT_SUBSYSTEMS]
        return run_smoke_tests(allowed, budget=budget, working_dir=self.working_dir)

    def diag_get_logs(self, query: Optional[Dict[str, Any]] = None, limit: int = 200) -> Dict[str, Any]:
        return query_logs(query=query, limit=limit, working_dir=self.working_dir)

    def diag_get_metrics(self, window_min: int = 60) -> Dict[str, Any]:
        payload = query_logs(limit=2000, working_dir=self.working_dir)
        now = time.time()
        threshold = now - max(1, int(window_min)) * 60
        durations: List[float] = []
        cache_hits = 0
        cache_total = 0
        budget_events = 0
        logs = []
        for row in payload.get("rows", []):
            ts = float(row.get("ts", 0))
            if ts < threshold:
                continue
            logs.append(row)
            duration = row.get("duration_ms")
            if isinstance(duration, (int, float)):
                durations.append(float(duration))
            if row.get("module") == "diagnostics.smoke" and "cache_size" in row:
                cache_total += 1
                if row.get("cache_size"):
                    cache_hits += 1
            if row.get("module") == "diagnostics.preflight" and row.get("op") == "gpu":
                budget_events += 1
        metrics = {}
        if durations:
            metrics["duration_ms"] = {
                "p50": round(statistics.median(durations), 2),
                "p90": round(statistics.quantiles(durations, n=10)[8], 2) if len(durations) >= 10 else round(max(durations), 2),
                "p95": round(statistics.quantiles(durations, n=20)[18], 2) if len(durations) >= 20 else round(max(durations), 2),
            }
        if cache_total:
            metrics["cache_hit_ratio"] = round(cache_hits / cache_total, 3)
        metrics["gpu_checks"] = budget_events
        return {"window_min": window_min, "metrics": metrics, "samples": len(logs)}

    def diag_sql(self, view: str, where: Optional[Dict[str, Any]] = None, limit: int = 50) -> Dict[str, Any]:
        view = (view or "").lower()
        limit = max(1, min(500, int(limit)))
        rows: List[Dict[str, Any]]
        if view == "preflight_checks":
            snapshot = load_snapshot("preflight", working_dir=self.working_dir) or {}
            rows = [
                {
                    "name": check.get("name"),
                    "ok": check.get("ok"),
                    "severity": check.get("severity"),
                    "message": check.get("message"),
                    "hint": check.get("hint"),
                    **(check.get("data") or {}),
                }
                for check in snapshot.get("checks", [])
            ]
        elif view == "smoke_checks":
            snapshot = load_snapshot("smoke", working_dir=self.working_dir) or {}
            rows = [
                {
                    "name": check.get("name"),
                    "ok": check.get("ok"),
                    "severity": check.get("severity"),
                    "message": check.get("message"),
                    "hint": check.get("hint"),
                    "duration_ms": check.get("duration_ms"),
                    **(check.get("data") or {}),
                }
                for check in snapshot.get("checks", [])
            ]
        elif view == "log_events":
            payload = query_logs(limit=limit, working_dir=self.working_dir)
            rows = list(payload.get("rows", []))
        else:
            raise ValueError("Unsupported diagnostics view")
        if where:
            filtered = []
            for row in rows:
                include = True
                for key, value in where.items():
                    if str(row.get(key)) != str(value):
                        include = False
                        break
                if include:
                    filtered.append(row)
            rows = filtered
        return {"rows": rows[:limit], "count": min(len(rows), limit)}

    def diag_get_report(self) -> Dict[str, Any]:
        return build_report_snapshot(working_dir=self.working_dir)


__all__ = ["DiagnosticsTools"]
