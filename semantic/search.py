"""Semantic search helpers for embeddings and FTS queries."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .config import SemanticConfig
from .db import semantic_connection


@dataclass(slots=True)
class SemanticDocument:
    """In-memory representation of a semantic document."""

    doc_id: int
    drive_label: str
    path: str
    metadata: Dict[str, object]
    embedding: List[float]


@dataclass(slots=True)
class SemanticSearchResult:
    """Normalized search result row."""

    path: str
    drive_label: str
    score: float
    mode: str
    snippet: Optional[str]
    metadata: Dict[str, object]

    def as_dict(self, rank: int) -> Dict[str, object]:
        payload = {
            "rank": rank,
            "path": self.path,
            "drive_label": self.drive_label,
            "score": round(float(self.score), 6),
            "mode": self.mode,
            "metadata": self.metadata,
        }
        if self.snippet:
            payload["snippet"] = self.snippet
        return payload


class SemanticSearcher:
    """Execute ANN, FTS5, or hybrid semantic queries."""

    def __init__(self, config: SemanticConfig) -> None:
        self.config = config

    def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        drive_label: Optional[str] = None,
        mode: str = "ann",
        hybrid: bool = False,
    ) -> Tuple[List[Dict[str, object]], int]:
        self.config.require_search_phase()
        sanitized = query.strip()
        if not sanitized:
            return [], 0
        mode = mode.lower()
        if mode not in {"ann", "text", "hybrid"}:
            mode = "ann"
        use_hybrid = hybrid or mode == "hybrid"
        base_mode = "ann" if mode == "hybrid" else mode
        documents = self._load_documents(drive_label)
        if not documents:
            return [], 0
        ann_scores: Dict[int, float] = {}
        text_scores: Dict[int, Tuple[float, Optional[str]]] = {}
        need_ann = base_mode == "ann" or use_hybrid
        need_text = base_mode == "text" or use_hybrid
        if need_ann:
            ann_scores = self._ann_scores(sanitized, documents)
        if need_text:
            text_scores = self._text_scores(sanitized, drive_label)
        scored = self._combine_scores(documents, ann_scores, text_scores, base_mode, use_hybrid)
        paginated = scored[offset : offset + limit]
        return [row.as_dict(rank=i + 1 + offset) for i, row in enumerate(paginated)], len(scored)

    def _load_documents(self, drive_label: Optional[str]) -> List[SemanticDocument]:
        documents: List[SemanticDocument] = []
        with semantic_connection(self.config.working_dir) as conn:
            sql = "SELECT id, drive_label, path, metadata, embedding FROM semantic_documents"
            params: List[object] = []
            if drive_label:
                sql += " WHERE drive_label = ?"
                params.append(drive_label)
            cursor = conn.execute(sql, params)
            for row in cursor.fetchall():
                try:
                    embedding = json.loads(row["embedding"] or "[]")
                except json.JSONDecodeError:
                    embedding = []
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                documents.append(
                    SemanticDocument(
                        doc_id=int(row["id"]),
                        drive_label=row["drive_label"],
                        path=row["path"],
                        metadata=metadata,
                        embedding=list(map(float, embedding)),
                    )
                )
        return documents

    def _ann_scores(self, query: str, docs: Sequence[SemanticDocument]) -> Dict[int, float]:
        query_vec = _normalize_vector([hash(token) % 997 for token in query.lower().split() or [query.lower()]])
        scores: Dict[int, float] = {}
        for doc in docs:
            if not doc.embedding:
                continue
            sim = _cosine_similarity(query_vec, doc.embedding)
            scores[doc.doc_id] = sim
        return scores

    def _text_scores(
        self, query: str, drive_label: Optional[str]
    ) -> Dict[int, Tuple[float, Optional[str]]]:
        tokens = [token for token in query.strip().split() if token]
        if not tokens:
            return {}
        fts_query = " AND ".join(f'"{token}"' for token in tokens)
        results: Dict[int, Tuple[float, Optional[str]]] = {}
        with semantic_connection(self.config.working_dir) as conn:
            if drive_label:
                cursor = conn.execute(
                    """
                    SELECT rowid, snippet(semantic_documents_fts, 0, '<b>', '</b>') AS snippet,
                           bm25(semantic_documents_fts) AS rank
                    FROM semantic_documents_fts
                    WHERE semantic_documents_fts MATCH ? AND drive_label = ?
                    ORDER BY rank ASC
                    """,
                    (fts_query, drive_label),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT rowid, snippet(semantic_documents_fts, 0, '<b>', '</b>') AS snippet,
                           bm25(semantic_documents_fts) AS rank
                    FROM semantic_documents_fts
                    WHERE semantic_documents_fts MATCH ?
                    ORDER BY rank ASC
                    """,
                    (fts_query,),
                )
            for row in cursor.fetchall():
                rank = float(row["rank"]) if row["rank"] is not None else 0.0
                score = 1.0 / (1.0 + max(rank, 0.0))
                results[int(row["rowid"])] = (score, row["snippet"])
        return results

    def _combine_scores(
        self,
        docs: Sequence[SemanticDocument],
        ann_scores: Dict[int, float],
        text_scores: Dict[int, Tuple[float, Optional[str]]],
        mode: str,
        use_hybrid: bool,
    ) -> List[SemanticSearchResult]:
        weight = float(self.config.hybrid_weight)
        combined: List[SemanticSearchResult] = []
        for doc in docs:
            ann_score = ann_scores.get(doc.doc_id)
            text_entry = text_scores.get(doc.doc_id)
            text_score = text_entry[0] if text_entry else None
            snippet = text_entry[1] if text_entry else None
            final_score: Optional[float]
            result_mode = mode
            if use_hybrid and ann_score is not None and text_score is not None:
                final_score = ann_score * weight + text_score * (1.0 - weight)
                result_mode = "hybrid"
            elif mode == "ann":
                final_score = ann_score
            elif mode == "text":
                final_score = text_score
            else:
                final_score = ann_score
            if final_score is None:
                continue
            combined.append(
                SemanticSearchResult(
                    path=doc.path,
                    drive_label=doc.drive_label,
                    score=float(final_score),
                    mode=result_mode,
                    snippet=snippet,
                    metadata=doc.metadata,
                )
            )
        combined.sort(key=lambda row: row.score, reverse=True)
        return combined


def _normalize_vector(values: Iterable[float]) -> List[float]:
    vec = [float(v) for v in values]
    if not vec:
        return []
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return [0.0 for _ in vec]
    return [v / norm for v in vec]


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    length = min(len(a), len(b))
    dot = sum(float(a[i]) * float(b[i]) for i in range(length))
    norm_a = math.sqrt(sum(float(v) * float(v) for v in a))
    norm_b = math.sqrt(sum(float(v) * float(v) for v in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
