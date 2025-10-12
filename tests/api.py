"""Public helpers for invoking the VideoCatalog smoke suite."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence

from core.paths import get_exports_dir, resolve_working_dir

from . import fixtures
from .smoke_runner import SmokeSuiteResult, TestsSettings, run_smoke_suite

__all__ = [
    "load_tests_settings",
    "prepare_fixture_data",
    "run_smoke",
    "open_last_report",
    "orchestrator_gate_reason",
]


def load_tests_settings(payload: Dict[str, object] | None) -> TestsSettings:
    section = payload.get("tests") if isinstance(payload, dict) else {}
    if not isinstance(section, dict):
        section = {}
    timeout_section = section.get("timeouts_s") if isinstance(section.get("timeouts_s"), dict) else {}
    fixtures_section = section.get("fixtures") if isinstance(section.get("fixtures"), dict) else {}
    gpu_tests = section.get("gpu_required_tests") or []
    report_formats = section.get("report_formats") or ["markdown", "junit"]
    settings = TestsSettings()
    settings.enable = bool(section.get("enable", settings.enable))
    settings.gate_on_fail = bool(section.get("gate_on_fail", settings.gate_on_fail))
    try:
        settings.timeouts["default"] = int(timeout_section.get("default", settings.timeouts["default"]))
    except Exception:
        pass
    try:
        settings.timeouts["gpu"] = int(timeout_section.get("gpu", settings.timeouts.get("gpu", 30)))
    except Exception:
        pass
    settings.fixtures_regen = bool(fixtures_section.get("regen_on_mismatch", False))
    settings.gpu_required_tests = {str(name) for name in gpu_tests if str(name)}
    settings.report_formats = tuple(str(fmt) for fmt in report_formats)
    return settings


def prepare_fixture_data(settings: TestsSettings) -> fixtures.FixtureResult:
    return fixtures.prepare_fixtures(force=settings.fixtures_regen)


def run_smoke(
    settings_data: Dict[str, object] | None,
    *,
    only: Optional[Iterable[str]] = None,
) -> SmokeSuiteResult:
    settings = load_tests_settings(settings_data)
    if not settings.enable:
        raise RuntimeError("Smoke tests are disabled via settings.json (tests.enable=false).")
    suite = run_smoke_suite(settings=settings, only=only)
    _record_last_run(suite)
    _update_gate(suite, settings)
    return suite


def open_last_report() -> Optional[Path]:
    working_dir = resolve_working_dir()
    testruns_dir = get_exports_dir(working_dir) / "testruns"
    if not testruns_dir.exists():
        return None
    candidates = [path for path in testruns_dir.iterdir() if path.is_dir()]
    for directory in sorted(candidates, reverse=True):
        summary = directory / "summary.md"
        if summary.exists():
            return summary
    return None


def orchestrator_gate_reason(
    settings_data: Dict[str, object] | None,
    *,
    working_dir: Optional[Path] = None,
) -> Optional[str]:
    settings = load_tests_settings(settings_data)
    if not settings.enable or not settings.gate_on_fail:
        return None
    gate_path = _gate_path(working_dir or resolve_working_dir())
    if not gate_path.exists():
        return None
    try:
        payload = json.loads(gate_path.read_text(encoding="utf-8"))
    except Exception:
        return "Smoke tests gate is active (corrupted state)."
    status = payload.get("status")
    timestamp = payload.get("timestamp")
    reason = payload.get("reason", "Smoke tests are failing")
    if timestamp:
        return f"{reason} — last run {timestamp}" if status else reason
    return reason


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _record_last_run(suite: SmokeSuiteResult) -> None:
    working_dir = resolve_working_dir()
    testruns_dir = get_exports_dir(working_dir) / "testruns"
    testruns_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": suite.timestamp,
        "status": suite.status,
        "run_dir": suite.run_dir.as_posix(),
        "results": [
            {
                "name": result.name,
                "status": result.status,
                "message": result.message,
                "duration_s": result.duration_s,
            }
            for result in suite.results
        ],
    }
    (testruns_dir / "last_run.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _gate_path(working_dir: Path) -> Path:
    return get_exports_dir(working_dir) / "testruns" / "gate.json"


def _update_gate(suite: SmokeSuiteResult, settings: TestsSettings) -> None:
    working_dir = resolve_working_dir()
    gate_path = _gate_path(working_dir)
    if suite.status == "FAIL" and settings.gate_on_fail:
        payload = {
            "status": suite.status,
            "timestamp": suite.timestamp,
            "reason": "Smoke tests failing",
            "summary": {
                "run_dir": suite.run_dir.as_posix(),
                "failures": [
                    {
                        "name": result.name,
                        "message": result.message,
                    }
                    for result in suite.results
                    if result.status == "FAIL"
                ],
            },
        }
        gate_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    else:
        gate_path.unlink(missing_ok=True)

