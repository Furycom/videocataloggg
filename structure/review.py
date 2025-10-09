"""Manual review queue helpers."""
from __future__ import annotations

from typing import Dict, List

from .types import ConfidenceBreakdown, FolderAnalysis, VerificationSignals


def build_reasons(analysis: FolderAnalysis, breakdown: ConfidenceBreakdown) -> List[str]:
    reasons = list(breakdown.reasons)
    if analysis.kind != "movie":
        reasons.append(f"Classified as {analysis.kind} based on folder contents.")
    return reasons


def build_questions(
    analysis: FolderAnalysis,
    verification: VerificationSignals,
) -> List[Dict[str, str]]:
    questions: List[Dict[str, str]] = []
    if verification.candidates:
        top = verification.candidates[0]
        if top.candidate_id:
            questions.append(
                {
                    "action": "confirm_candidate",
                    "label": f"Confirm {top.title or top.candidate_id}",
                    "candidate_id": str(top.candidate_id),
                    "source": top.source,
                }
            )
    if analysis.nfo_ids:
        questions.append(
            {
                "action": "review_nfo",
                "label": "Inspect existing .nfo metadata",
            }
        )
    questions.append(
        {
            "action": "open_folder",
            "label": "Open folder",
        }
    )
    if verification.candidates:
        questions.append(
            {
                "action": "alt_tmdb_search",
                "label": "Search TMDb with alternative title",
                "query": verification.candidates[0].title or analysis.folder_path.name,
            }
        )
    return questions


__all__ = ["build_reasons", "build_questions"]
