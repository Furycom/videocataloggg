from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Optional

from core.paths import resolve_working_dir

from .checks import HealthReport, run_checks
from .store import HealthStore


def run_health_checks(working_dir: Optional[Path] = None, persist: bool = True) -> HealthReport:
    base = working_dir or resolve_working_dir()
    report = run_checks(base)
    if persist:
        HealthStore(base).save(report)
    return report


def run_quick_health_pass(working_dir: Optional[Path] = None) -> HealthReport:
    return run_health_checks(working_dir=working_dir, persist=True)


def format_report(report: HealthReport, *, include_details: bool = True) -> str:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.ts))
    lines = [f"[{timestamp}] major={report.summary.major} minor={report.summary.minor}"]
    for item in report.items:
        base = f" - {item.severity.value}:{item.code} @ {item.where} :: {item.hint}"
        if include_details and item.details:
            base += f" ({item.details})"
        lines.append(base)
    if len(report.items) == 0:
        lines.append(" - OK: no findings")
    return "\n".join(lines)


def cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run VideoCatalog health checks")
    parser.add_argument("--json", action="store_true", help="Output report as JSON")
    parser.add_argument("--no-persist", action="store_true", help="Do not persist report")
    parser.add_argument(
        "--working-dir",
        type=Path,
        default=None,
        help="Override working directory",
    )
    args = parser.parse_args(argv)
    report = run_health_checks(working_dir=args.working_dir, persist=not args.no_persist)
    if args.json:
        payload = {
            "ts": report.ts,
            "summary": {
                "major": report.summary.major,
                "minor": report.summary.minor,
            },
            "items": [
                {
                    "severity": item.severity.value,
                    "code": item.code,
                    "where": item.where,
                    "hint": item.hint,
                    "details": item.details,
                }
                for item in report.items
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli())
