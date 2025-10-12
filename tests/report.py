"""Reporting helpers for VideoCatalog smoke suite."""
from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Sequence, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .smoke_runner import SmokeSuiteResult, SmokeTestResult

__all__ = ["write_reports"]


def write_reports(run_dir: Path, suite: "SmokeSuiteResult", *, formats: Sequence[str]) -> None:
    if "markdown" in formats:
        _write_markdown(run_dir / "summary.md", suite)
    if "junit" in formats:
        _write_junit(run_dir / "junit.xml", suite)


def _write_markdown(path: Path, suite: "SmokeSuiteResult") -> None:
    lines = [
        f"# VideoCatalog Smoke Tests — {suite.timestamp}",
        "",
        f"**Overall status:** {suite.status}",
        "",
    ]
    totals = _totals(suite.results)
    lines.append(
        " | ".join(
            [
                f"Pass: {totals['pass']}",
                f"Fail: {totals['fail']}",
                f"Skip: {totals['skip']}",
            ]
        )
    )
    lines.append("")
    if suite.warnings:
        lines.append("## Warnings")
        for warning in suite.warnings:
            lines.append(f"- {warning}")
        lines.append("")
    lines.append("## Test Results")
    for result in suite.results:
        lines.append(
            f"### {result.name} — {result.status} ({result.duration_s:.2f}s)"
        )
        if result.message:
            lines.append(result.message)
        if result.diff_path:
            rel = result.diff_path.relative_to(suite.run_dir)
            lines.append(f"Diff: `{rel.as_posix()}`")
        if result.diagnostics:
            lines.append("Diagnostics:")
            for key, value in result.diagnostics.items():
                lines.append(f"- {key}: {value}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_junit(path: Path, suite: "SmokeSuiteResult") -> None:
    totals = _totals(suite.results)
    root = ET.Element(
        "testsuite",
        {
            "name": "videocatalog.smoke",
            "tests": str(len(suite.results)),
            "failures": str(totals["fail"]),
            "skipped": str(totals["skip"]),
            "time": f"{sum(result.duration_s for result in suite.results):.2f}",
        },
    )
    for result in suite.results:
        case = ET.SubElement(
            root,
            "testcase",
            {
                "name": result.name,
                "classname": "videocatalog.smoke",
                "time": f"{result.duration_s:.3f}",
            },
        )
        if result.status == "SKIP":
            skipped = ET.SubElement(case, "skipped")
            skipped.text = html.escape(result.message or "Skipped")
        elif result.status == "FAIL":
            failure = ET.SubElement(
                case,
                "failure",
                {
                    "message": result.message or "Failure",
                },
            )
            if result.diff_path:
                failure.text = html.escape(result.diff_path.as_posix())
        # Pass results need no extra nodes.
    tree = ET.ElementTree(root)
    path.write_text(
        ET.tostring(root, encoding="unicode", xml_declaration=True),
        encoding="utf-8",
    )


def _totals(results: Iterable["SmokeTestResult"]) -> dict[str, int]:
    totals = {"pass": 0, "fail": 0, "skip": 0}
    for result in results:
        if result.status == "PASS":
            totals["pass"] += 1
        elif result.status == "FAIL":
            totals["fail"] += 1
        else:
            totals["skip"] += 1
    return totals

