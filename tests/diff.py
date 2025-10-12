"""Utility helpers to compare smoke test outputs against goldens."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Sequence

__all__ = [
    "DiffResult",
    "compare_json",
    "compare_csv",
    "save_diff_artifacts",
]


@dataclass(slots=True)
class DiffResult:
    match: bool
    summary: str
    details: List[str] = field(default_factory=list)
    expected: Any | None = None
    actual: Any | None = None


def compare_json(golden_path: Path, actual: Any) -> DiffResult:
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    differences = _diff_values(expected, actual)
    if not differences:
        return DiffResult(match=True, summary="Values match", expected=expected, actual=actual)
    summary = differences[0]
    if len(differences) > 1:
        summary += f" (+{len(differences) - 1} diffs)"
    return DiffResult(
        match=False,
        summary=summary,
        details=differences[:20],
        expected=expected,
        actual=actual,
    )


def compare_csv(golden_path: Path, rows: Sequence[Mapping[str, Any]]) -> DiffResult:
    with golden_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_rows = [dict(row) for row in reader]
    actual_rows = [dict(row) for row in rows]
    expected_serial = json.dumps(expected_rows, sort_keys=True)
    actual_serial = json.dumps(actual_rows, sort_keys=True)
    if expected_serial == actual_serial:
        return DiffResult(match=True, summary="CSV rows match", expected=expected_rows, actual=actual_rows)
    differences = _diff_values(expected_rows, actual_rows)
    summary = differences[0] if differences else "CSV rows differ"
    if len(differences) > 1:
        summary += f" (+{len(differences) - 1} diffs)"
    return DiffResult(
        match=False,
        summary=summary,
        details=differences[:20],
        expected=expected_rows,
        actual=actual_rows,
    )


def save_diff_artifacts(diff_dir: Path, name: str, result: DiffResult) -> Path:
    diff_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": result.summary,
        "details": result.details,
        "expected": result.expected,
        "actual": result.actual,
    }
    path = diff_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _diff_values(expected: Any, actual: Any, *, trail: str = "root") -> List[str]:
    diffs: List[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_keys = set(expected)
        actual_keys = set(actual)
        for missing in sorted(expected_keys - actual_keys):
            diffs.append(f"{trail}.{missing}: missing in actual output")
        for extra in sorted(actual_keys - expected_keys):
            diffs.append(f"{trail}.{extra}: unexpected key in actual output")
        for key in sorted(expected_keys & actual_keys):
            diffs.extend(_diff_values(expected[key], actual[key], trail=f"{trail}.{key}"))
        return diffs
    if isinstance(expected, list) and isinstance(actual, list):
        min_len = min(len(expected), len(actual))
        for index in range(min_len):
            diffs.extend(_diff_values(expected[index], actual[index], trail=f"{trail}[{index}]"))
        if len(expected) != len(actual):
            diffs.append(
                f"{trail}: length mismatch expected={len(expected)} actual={len(actual)}"
            )
        return diffs
    if expected != actual:
        diffs.append(f"{trail}: expected={expected!r} actual={actual!r}")
    return diffs

