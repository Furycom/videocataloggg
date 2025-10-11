"""Comparison helpers for subtitle summaries and official plots."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set

import numpy as np

LOGGER = logging.getLogger("videocatalog.textverify.compare")

_PROPER_NOUN_RE = re.compile(r"\b([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)")
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "your",
    "their",
    "after",
    "before",
    "when",
    "while",
    "over",
    "under",
    "that",
    "this",
    "these",
    "those",
}


@dataclass(slots=True)
class ComparisonScores:
    semantic: float
    ner_overlap: float
    keyword_overlap: float
    aggregated: float


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    sim = float(np.dot(a, b) / denom)
    return max(0.0, min(1.0, (sim + 1.0) / 2.0)) if sim < 0 else min(1.0, sim)


def _load_spacy_model() -> Optional[object]:
    try:
        import spacy  # type: ignore

        try:
            return spacy.load("en_core_web_sm")
        except Exception:
            return spacy.blank("en")
    except Exception:  # pragma: no cover - optional dependency
        return None


class _EntityExtractor:
    def __init__(self) -> None:
        self._spacy = _load_spacy_model()

    def extract(self, text: str) -> Set[str]:
        if self._spacy is None:
            return self._heuristic_entities(text)
        try:
            doc = self._spacy(text)
        except Exception as exc:  # pragma: no cover - runtime dependent
            LOGGER.debug("spaCy NER failed: %s", exc)
            return self._heuristic_entities(text)
        entities: Set[str] = set()
        for ent in getattr(doc, "ents", []):
            label = getattr(ent, "label_", "")
            if label not in {"PERSON", "ORG", "WORK_OF_ART", "GPE"}:
                continue
            value = str(ent.text).strip()
            if not value:
                continue
            entities.add(value.lower())
        if entities:
            return entities
        return self._heuristic_entities(text)

    def _heuristic_entities(self, text: str) -> Set[str]:
        matches = _PROPER_NOUN_RE.findall(text)
        results = {match.lower() for match in matches if match.lower() not in _STOPWORDS}
        return {value for value in results if len(value) > 2}


_ENTITY_EXTRACTOR = _EntityExtractor()


def named_entities(text: str) -> Set[str]:
    return _ENTITY_EXTRACTOR.extract(text)


def keyword_overlap(a: Sequence[str], b: Sequence[str]) -> float:
    set_a = {item.strip().lower() for item in a if item.strip()}
    set_b = {item.strip().lower() for item in b if item.strip()}
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return intersection / union


def aggregate_scores(
    semantic: float,
    ner_overlap: float,
    keyword_overlap_score: float,
    *,
    weight_semantic: float,
    weight_ner: float,
    weight_keywords: float,
) -> ComparisonScores:
    semantic_clamped = max(0.0, min(1.0, semantic))
    ner_clamped = max(0.0, min(1.0, ner_overlap))
    keyword_clamped = max(0.0, min(1.0, keyword_overlap_score))
    total_weight = max(1e-6, weight_semantic + weight_ner + weight_keywords)
    weighted = (
        semantic_clamped * weight_semantic
        + ner_clamped * weight_ner
        + keyword_clamped * weight_keywords
    ) / total_weight
    aggregated = max(0.0, min(1.0, weighted))
    return ComparisonScores(
        semantic=semantic_clamped,
        ner_overlap=ner_clamped,
        keyword_overlap=keyword_clamped,
        aggregated=aggregated,
    )


__all__ = [
    "ComparisonScores",
    "aggregate_scores",
    "cosine_similarity",
    "keyword_overlap",
    "named_entities",
]
