from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

from .config import AssistantSettings

LOGGER = logging.getLogger("videocatalog.assistant.rag")


@dataclass(slots=True)
class DocumentHit:
    doc_id: str
    score: float
    text: str
    metadata: Dict[str, object]


class VectorIndex:
    """Lazy semantic index backed by FAISS or hnswlib."""

    def __init__(self, settings: AssistantSettings, db_path: Path, working_dir: Path) -> None:
        self.settings = settings
        self.db_path = db_path
        self.working_dir = working_dir
        self.index_dir = working_dir / "vectors"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.index_dir / "catalog.index"
        self.meta_path = self.index_dir / "catalog_meta.json"
        self._lock = threading.Lock()
        self._index = None
        self._backend = settings.rag.index
        self._dim = 384
        self._meta: Dict[int, Dict[str, object]] = {}
        self._embedder = None

    # ------------------------------------------------------------------
    def ensure_ready(self, force: bool = False) -> None:
        if not self.settings.rag.enable:
            return
        with self._lock:
            if not force and self._index is not None:
                return
            if not force and self.index_path.exists() and self.meta_path.exists():
                self._load_index()
                return
            self._rebuild_index()

    def refresh(self) -> None:
        with self._lock:
            self._rebuild_index()

    # ------------------------------------------------------------------
    def search(self, query: str, *, top_k: Optional[int] = None, min_score: Optional[float] = None) -> List[DocumentHit]:
        if not self.settings.rag.enable:
            return []
        self.ensure_ready()
        with self._lock:
            if self._index is None:
                return []
            embedder = self._ensure_embedder()
            vector = embedder.encode([query], convert_to_numpy=True, normalize_embeddings=True)[0]
            k = top_k or self.settings.rag.top_k
            labels, distances = self._knn_query(vector, k)
            results: List[DocumentHit] = []
            threshold = min_score if min_score is not None else self.settings.rag.min_score
            for label, dist in zip(labels[0], distances[0]):
                score = float(1 - dist)
                if score < threshold:
                    continue
                meta = dict(self._meta.get(int(label), {}))
                results.append(
                    DocumentHit(
                        doc_id=str(meta.get("doc_id", label)),
                        score=score,
                        text=str(meta.get("text", "")),
                        metadata=meta,
                    )
                )
            return results

    # ------------------------------------------------------------------
    def _load_index(self) -> None:
        backend = self.settings.rag.index
        if backend == "hnswlib":
            try:
                import hnswlib
            except Exception as exc:
                LOGGER.error("Failed to import hnswlib: %s", exc)
                return
            index = hnswlib.Index(space="cosine", dim=self._dim)
            index.load_index(str(self.index_path))
            with self.meta_path.open("r", encoding="utf-8") as fh:
                meta_payload = json.load(fh)
            self._meta = {int(k): v for k, v in meta_payload.get("entries", {}).items()}
            self._index = index
            LOGGER.info("Assistant RAG: loaded hnswlib index with %d entries", len(self._meta))
            return
        else:
            try:
                import faiss
            except Exception as exc:
                LOGGER.error("Failed to import faiss: %s", exc)
                return
            index = faiss.read_index(str(self.index_path))
            with self.meta_path.open("r", encoding="utf-8") as fh:
                meta_payload = json.load(fh)
            self._meta = {int(k): v for k, v in meta_payload.get("entries", {}).items()}
            self._index = index
            LOGGER.info("Assistant RAG: loaded FAISS index with %d entries", len(self._meta))

    def _rebuild_index(self) -> None:
        backend = self.settings.rag.index
        documents = list(self._collect_documents())
        if not documents:
            LOGGER.warning("Assistant RAG: no documents to index")
            self._index = None
            self._meta = {}
            return
        embedder = self._ensure_embedder()
        texts = [doc.text for doc in documents]
        vectors = embedder.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        if backend == "hnswlib":
            index = self._build_hnsw(vectors)
        else:
            index = self._build_faiss(vectors)
        if index is None:
            return
        meta_entries = {
            i: {"doc_id": doc.doc_id, "text": doc.text, **self._serialize_meta(doc.metadata)}
            for i, doc in enumerate(documents)
        }
        with self.meta_path.open("w", encoding="utf-8") as fh:
            json.dump({"entries": meta_entries}, fh, indent=2)
        self._meta = meta_entries
        if isinstance(index, _SimpleVectorIndex):
            # Remove stale on-disk indexes from previous backends to avoid confusion.
            try:
                if self.index_path.exists():
                    self.index_path.unlink()
            except Exception:
                LOGGER.debug("Assistant RAG: failed to clean legacy index file at %s", self.index_path)
            self._index = index
            LOGGER.info(
                "Assistant RAG: rebuilt in-memory simple index with %d entries", len(documents)
            )
            return
        if backend == "hnswlib":
            index.save_index(str(self.index_path))
        else:
            import faiss

            faiss.write_index(index, str(self.index_path))
        self._index = index
        LOGGER.info("Assistant RAG: rebuilt %s index with %d entries", backend, len(documents))

    # ------------------------------------------------------------------
    def _collect_documents(self) -> Iterable[DocumentHit]:
        if not self.db_path.exists():
            LOGGER.warning("Assistant RAG: catalog database missing at %s", self.db_path)
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = self._existing_tables(conn)
            collectors = [
                self._collect_docs_preview,
                self._collect_textlite,
                self._collect_music,
                self._collect_inventory,
            ]
            for collector in collectors:
                yield from collector(conn, tables)

    def _collect_docs_preview(self, conn: sqlite3.Connection, tables: Sequence[str]) -> Iterable[DocumentHit]:
        if "docs_preview" not in tables:
            return []
        rows = conn.execute(
            "SELECT id, title, summary, keywords, path FROM docs_preview WHERE summary IS NOT NULL ORDER BY id LIMIT 5000"
        )
        for row in rows:
            text = "\n".join([part for part in (row["title"], row["summary"], row["keywords"]) if part])
            yield DocumentHit(
                doc_id=f"doc:{row['id']}",
                score=1.0,
                text=text[:2000],
                metadata={"type": "doc", "path": row["path"], "title": row["title"]},
            )

    def _collect_textlite(self, conn: sqlite3.Connection, tables: Sequence[str]) -> Iterable[DocumentHit]:
        if "textlite_preview" not in tables:
            return []
        rows = conn.execute(
            "SELECT id, path, head_excerpt, mid_excerpt, tail_excerpt FROM textlite_preview WHERE head_excerpt IS NOT NULL"
        )
        for row in rows:
            text = "\n".join(
                part
                for part in (
                    row["head_excerpt"],
                    row["mid_excerpt"],
                    row["tail_excerpt"],
                )
                if part
            )
            yield DocumentHit(
                doc_id=f"text:{row['id']}",
                score=1.0,
                text=text[:2000],
                metadata={"type": "text", "path": row["path"]},
            )

    def _collect_music(self, conn: sqlite3.Connection, tables: Sequence[str]) -> Iterable[DocumentHit]:
        if "music_minimal" not in tables:
            return []
        rows = conn.execute(
            "SELECT rowid AS id, title, artist, album, year, path FROM music_minimal ORDER BY rowid LIMIT 15000"
        )
        for row in rows:
            text = " - ".join([part for part in (row["title"], row["artist"], row["album"]) if part])
            text = f"{text} ({row['year']})" if row["year"] else text
            yield DocumentHit(
                doc_id=f"music:{row['id']}",
                score=1.0,
                text=text[:2000],
                metadata={"type": "music", "path": row["path"]},
            )

    def _collect_inventory(self, conn: sqlite3.Connection, tables: Sequence[str]) -> Iterable[DocumentHit]:
        if "inventory_view" in tables:
            rows = conn.execute(
                "SELECT inventory_id, title, year, type, drive_label FROM inventory_view ORDER BY inventory_id LIMIT 10000"
            )
            for row in rows:
                parts = [row["title"] or "", str(row["year"] or ""), row["type"] or "", row["drive_label"] or ""]
                text = " ".join(p for p in parts if p)
                yield DocumentHit(
                    doc_id=f"inventory:{row['inventory_id']}",
                    score=1.0,
                    text=text[:2000],
                    metadata={"type": row["type"], "drive": row["drive_label"], "title": row["title"]},
                )

        return []

    @staticmethod
    def _existing_tables(conn: sqlite3.Connection) -> List[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return [row[0] for row in rows]

    def _ensure_embedder(self):
        if self._embedder is None:
            model_name = self.settings.rag.embed_model or "bge-small-en"
            if model_name.startswith("stub:"):
                LOGGER.info("Assistant RAG: using stub embedding model %s", model_name)
                self._embedder = _StubEmbeddingModel(model_name)
            else:
                from sentence_transformers import SentenceTransformer

                LOGGER.info("Assistant RAG: loading embeddings model %s", model_name)
                self._embedder = SentenceTransformer(model_name)
            self._dim = int(self._embedder.get_sentence_embedding_dimension())
        return self._embedder

    def _build_hnsw(self, vectors: np.ndarray):
        try:
            import hnswlib
        except Exception as exc:
            LOGGER.warning("Assistant RAG: hnswlib unavailable (%s); using simple index", exc)
            return _SimpleVectorIndex(vectors)
        num = len(vectors)
        index = hnswlib.Index(space="cosine", dim=vectors.shape[1])
        index.init_index(max_elements=num, ef_construction=200, M=48)
        index.add_items(vectors, ids=np.arange(num))
        index.set_ef(64)
        return index

    def _build_faiss(self, vectors: np.ndarray):
        try:
            import faiss
        except Exception as exc:
            LOGGER.warning("Assistant RAG: faiss unavailable (%s); using simple index", exc)
            return _SimpleVectorIndex(vectors)
        vectors = vectors.astype("float32")
        num, dim = vectors.shape
        index = faiss.IndexFlatIP(dim)
        index.add(vectors)
        return index

    def _knn_query(self, vector: np.ndarray, k: int):
        if isinstance(self._index, _SimpleVectorIndex):
            scores, labels = self._index.search(vector, k=k)
            return labels, 1 - scores
        if self.settings.rag.index == "hnswlib":
            labels, distances = self._index.knn_query(vector, k=k)
            return labels, distances
        import numpy as np

        vector = np.array([vector], dtype="float32")
        distances, labels = self._index.search(vector, k)
        # Align signature with hnswlib (labels first, distances second)
        return labels.astype(int), 1 - distances

    @staticmethod
    def _serialize_meta(meta: Dict[str, object]) -> Dict[str, object]:
        normalized: Dict[str, object] = {}
        for key, value in meta.items():
            if isinstance(value, Path):
                normalized[key] = str(value)
            elif isinstance(value, (list, tuple)):
                normalized[key] = [str(item) if isinstance(item, Path) else item for item in value]
            else:
                normalized[key] = value
        return normalized
class _StubEmbeddingModel:
    """Deterministic embedding stub used for smoke tests."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._dim = 96

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    def encode(
        self,
        texts: Sequence[str],
        *,
        convert_to_numpy: bool = True,
        normalize_embeddings: bool = True,
    ) -> np.ndarray:
        vectors: List[np.ndarray] = []
        salt = self._name.encode("utf-8")
        for text in texts:
            blob = hashlib.sha256(salt + text.encode("utf-8")).digest()
            repeat = (self._dim + len(blob) - 1) // len(blob)
            combined = (blob * repeat)[: self._dim]
            vector = np.frombuffer(combined, dtype=np.uint8).astype("float32")
            vectors.append(vector)
        matrix = np.vstack(vectors)
        if normalize_embeddings:
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
        return matrix if convert_to_numpy else matrix.tolist()


class _SimpleVectorIndex:
    """Lightweight cosine similarity index used when faiss/hnswlib are unavailable."""

    def __init__(self, vectors: np.ndarray) -> None:
        normalized = vectors.astype("float32")
        norms = np.linalg.norm(normalized, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._vectors = normalized / norms

    def search(self, queries: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        if queries.ndim == 1:
            queries = np.expand_dims(queries, axis=0)
        queries = queries.astype("float32")
        norms = np.linalg.norm(queries, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normalized_queries = queries / norms
        scores = normalized_queries @ self._vectors.T
        top_idx = np.argsort(scores, axis=1)[:, ::-1][:, :k]
        top_scores = np.take_along_axis(scores, top_idx, axis=1)
        return top_scores, top_idx.astype(int)
