"""Confidence scoring utilities."""
from __future__ import annotations

from typing import Dict, List, Optional, TYPE_CHECKING

from .types import ConfidenceBreakdown, FolderAnalysis, VerificationSignals

if TYPE_CHECKING:  # pragma: no cover - typing helper
    from .service import StructureSettings

_PENALTY_MAP: Dict[str, float] = {
    "multiple_primary_videos": 0.10,
    "missing_year": 0.10,
    "missing_canonical_pattern": 0.05,
    "missing_nfo": 0.05,
    "invalid_year": 0.05,
    "no_primary_video": 0.20,
    "unreadable_folder": 0.20,
}


def _penalty_for(issue: str) -> float:
    for key, value in _PENALTY_MAP.items():
        if issue.startswith(key):
            return value
    return 0.03


def compute_confidence(
    analysis: FolderAnalysis,
    verification: VerificationSignals,
    settings: "StructureSettings",
    *,
    runtime_match: Optional[bool] = None,
) -> ConfidenceBreakdown:
    weights = settings.weights
    score = 0.0
    reasons: List[str] = []
    signals: Dict[str, float] = {}

    if analysis.canonical:
        score += weights.get("canon", 0.0)
        signals["canon"] = weights.get("canon", 0.0)
    else:
        signals["canon"] = 0.0
        reasons.append("Folder name does not follow Title (Year) pattern.")

    if analysis.nfo_ids:
        score += weights.get("nfo", 0.0)
        signals["nfo"] = weights.get("nfo", 0.0)
    else:
        signals["nfo"] = 0.0
        if analysis.kind == "movie":
            reasons.append("No .nfo file with IDs was detected.")

    if verification.oshash_match:
        score += weights.get("oshash", 0.0)
        signals["oshash"] = weights.get("oshash", 0.0)
    else:
        signals["oshash"] = 0.0

    name_weight = weights.get("name_match", 0.0)
    if verification.best_name_score >= 0.85:
        score += name_weight
        signals["name_match"] = name_weight
    else:
        incremental = name_weight * max(0.0, min(verification.best_name_score, 1.0))
        score += incremental
        signals["name_match"] = incremental
        if verification.best_name_score < 0.5:
            reasons.append("Name similarity to external candidates is weak.")

    runtime_weight = weights.get("runtime", 0.0)
    if runtime_match is True:
        score += runtime_weight
        signals["runtime"] = runtime_weight
    else:
        signals["runtime"] = 0.0

    penalty_total = 0.0
    for issue in analysis.issues:
        penalty = _penalty_for(issue)
        penalty_total += penalty
        reasons.append(f"Issue detected: {issue}")
    score = max(0.0, min(1.0, score - penalty_total))

    return ConfidenceBreakdown(confidence=score, reasons=reasons, signals=signals)


__all__ = ["compute_confidence"]
