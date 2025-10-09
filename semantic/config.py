"""Configuration helpers for the semantic indexing pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


class SemanticPhaseError(RuntimeError):
    """Raised when a semantic pipeline phase is disabled by configuration."""


@dataclass(slots=True)
class SemanticConfig:
    """Resolved semantic settings derived from ``settings.json``."""

    working_dir: Path
    vector_dim: int = 64
    hybrid_weight: float = 0.4
    index_phase: str = "manual"
    search_phase: str = "enabled"
    transcribe_phase: str = "manual"
    rebuild_chunk: int = 500

    @classmethod
    def from_settings(cls, working_dir: Path, settings: Dict[str, Any]) -> "SemanticConfig":
        semantic_settings = settings.get("semantic") if isinstance(settings, dict) else None
        if not isinstance(semantic_settings, dict):
            semantic_settings = {}
        vector_dim = int(semantic_settings.get("vector_dim") or 64)
        if vector_dim <= 0:
            vector_dim = 64
        hybrid_weight = float(semantic_settings.get("hybrid_weight") or 0.4)
        hybrid_weight = min(1.0, max(0.0, hybrid_weight))
        index_phase = str(semantic_settings.get("index_phase") or "manual").lower()
        search_phase = str(semantic_settings.get("search_phase") or "enabled").lower()
        transcribe_phase = str(semantic_settings.get("transcribe_phase") or "manual").lower()
        rebuild_chunk = int(semantic_settings.get("rebuild_chunk") or 500)
        if rebuild_chunk <= 0:
            rebuild_chunk = 500
        return cls(
            working_dir=working_dir,
            vector_dim=vector_dim,
            hybrid_weight=hybrid_weight,
            index_phase=index_phase,
            search_phase=search_phase,
            transcribe_phase=transcribe_phase,
            rebuild_chunk=rebuild_chunk,
        )

    def require_index_phase(self) -> None:
        if self.index_phase == "off":
            raise SemanticPhaseError("semantic indexing is disabled by settings")

    def require_search_phase(self) -> None:
        if self.search_phase == "off":
            raise SemanticPhaseError("semantic search is disabled by settings")

    def require_transcribe_phase(self) -> None:
        if self.transcribe_phase == "off":
            raise SemanticPhaseError("semantic transcription is disabled by settings")
