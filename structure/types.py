"""Shared dataclasses for structure profiling."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence


@dataclass(slots=True)
class VideoFile:
    """Basic information about a candidate primary video file."""

    path: Path
    size_bytes: int
    duration_seconds: Optional[float] = None
    basename: Optional[str] = None


@dataclass(slots=True)
class FolderAnalysis:
    """Raw facts collected from filesystem inspection."""

    folder_path: Path
    rel_path: str
    kind: str
    canonical: bool
    video_files: List[VideoFile] = field(default_factory=list)
    assets: Dict[str, object] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    nfo_files: List[Path] = field(default_factory=list)
    nfo_ids: Dict[str, str] = field(default_factory=dict)
    detected_year: Optional[int] = None

    @property
    def main_video(self) -> Optional[VideoFile]:
        if not self.video_files:
            return None
        primary = max(self.video_files, key=lambda item: item.size_bytes)
        return primary


@dataclass(slots=True)
class GuessResult:
    """Normalized GuessIt result for a path."""

    title: Optional[str] = None
    year: Optional[int] = None
    edition: Optional[str] = None
    raw: Dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class VerificationCandidate:
    """External candidate returned by TMDb, IMDb or OpenSubtitles."""

    source: str
    candidate_id: Optional[str]
    title: Optional[str]
    year: Optional[int]
    score: float
    extra: Dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class VerificationSignals:
    """Signals gathered from verification steps."""

    candidates: List[VerificationCandidate] = field(default_factory=list)
    best_name_score: float = 0.0
    oshash_match: Optional[Dict[str, object]] = None
    nfo_ids: Dict[str, str] = field(default_factory=dict)

    def top_candidates(self, limit: int) -> Sequence[VerificationCandidate]:
        return self.candidates[:limit]


@dataclass(slots=True)
class ConfidenceBreakdown:
    """Score contributions and reasoning."""

    confidence: float
    reasons: List[str] = field(default_factory=list)
    signals: Dict[str, float] = field(default_factory=dict)


