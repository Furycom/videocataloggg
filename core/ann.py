"""Approximate nearest neighbour index helpers."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

LOGGER = logging.getLogger("videocatalog.ann")

try:  # pragma: no cover - optional dependency guard
    import faiss  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    faiss = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    import hnswlib  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    hnswlib = None  # type: ignore


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


class ANNIndexManager:
    """Manage FAISS/HNSW indices for semantic vectors."""

    def __init__(self, working_dir: Path, shard_label: str, *, backend: str = "auto") -> None:
        self._dir = _ensure_dir(Path(working_dir) / "data" / "ann")
        safe_label = shard_label.replace(os.sep, "_")
        self._base = self._dir / safe_label
        self._meta_path = self._base.with_suffix(".json")
        self._vector_path = self._base.with_suffix(".npy")
        self._backend = self._resolve_backend(backend)
        self._index_path = self._base.with_suffix(f".{self._backend}")
        self._dim: Optional[int] = None
        self._paths: List[str] = []

    def _resolve_backend(self, backend: str) -> str:
        normalized = (backend or "auto").lower()
        if normalized in {"faiss", "hnsw"}:
            if normalized == "faiss" and faiss is not None:
                return "faiss"
            if normalized == "hnsw" and hnswlib is not None:
                return "hnsw"
        if faiss is not None:
            return "faiss"
        if hnswlib is not None:
            return "hnsw"
        LOGGER.info("ANN backends unavailable — falling back to brute-force search")
        return "bruteforce"

    def rebuild_from_db(
        self,
        connection,
        *,
        kinds: Sequence[str] = ("image", "video"),
    ) -> Dict[str, int]:
        cur = connection.cursor()
        placeholders = ",".join("?" for _ in kinds)
        query = f"SELECT path, vec, dim FROM features WHERE kind IN ({placeholders})"
        rows = cur.execute(query, tuple(kinds)).fetchall()
        paths: List[str] = []
        vectors: List[np.ndarray] = []
        for path, blob, dim in rows:
            if blob is None or dim is None:
                continue
            arr = np.frombuffer(blob, dtype=np.float32)
            if arr.size < int(dim):
                continue
            vector = arr[: int(dim)]
            if not np.any(vector):
                continue
            norm = np.linalg.norm(vector)
            if norm > 0:
                vector = vector / norm
            paths.append(path)
            vectors.append(vector.astype(np.float32, copy=False))
        if not vectors:
            self._clear()
            return {"vectors": 0, "backend": self._backend}
        matrix = np.stack(vectors, axis=0)
        self._store(paths, matrix)
        return {"vectors": len(paths), "backend": self._backend, "dim": int(matrix.shape[1])}

    def _store(self, paths: List[str], matrix: np.ndarray) -> None:
        self._paths = list(paths)
        self._dim = int(matrix.shape[1])
        np.save(self._vector_path, matrix.astype(np.float32, copy=False))
        meta = {
            "paths": self._paths,
            "backend": self._backend,
            "dim": self._dim,
        }
        self._meta_path.write_text(json.dumps(meta), encoding="utf-8")
        if self._backend == "faiss" and faiss is not None:
            index = faiss.IndexFlatIP(self._dim)
            index.add(matrix.astype(np.float32, copy=False))
            faiss.write_index(index, str(self._index_path))
        elif self._backend == "hnsw" and hnswlib is not None:
            index = hnswlib.Index(space="cosine", dim=self._dim)
            index.init_index(max_elements=len(self._paths), ef_construction=200, M=32)
            ids = np.arange(len(self._paths))
            index.add_items(matrix.astype(np.float32, copy=False), ids)
            index.save_index(str(self._index_path))
        else:
            try:
                if self._index_path.exists():
                    self._index_path.unlink()
            except Exception:
                pass

    def _clear(self) -> None:
        for path in [self._meta_path, self._vector_path, self._index_path]:
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
        self._paths = []
        self._dim = None

    def search(self, query: np.ndarray, top_k: int = 10) -> List[Tuple[str, float]]:
        if not isinstance(query, np.ndarray):
            query = np.asarray(query, dtype=np.float32)
        if query.ndim != 1:
            query = query.reshape(-1)
        norm = np.linalg.norm(query)
        if norm > 0:
            query = query / norm
        if self._backend == "faiss" and faiss is not None and self._index_path.exists():
            index = faiss.read_index(str(self._index_path))
            scores, ids = index.search(query.astype(np.float32)[None, :], top_k)
            return self._map_results(scores[0], ids[0])
        if self._backend == "hnsw" and hnswlib is not None and self._index_path.exists():
            index = hnswlib.Index(space="cosine", dim=query.size)
            index.load_index(str(self._index_path))
            labels, distances = index.knn_query(query.astype(np.float32), k=top_k)
            scores = 1.0 - distances[0]
            return self._map_results(scores, labels[0])
        if self._vector_path.exists() and self._meta_path.exists():
            matrix = np.load(self._vector_path)
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
            paths = data.get("paths", [])
            scores = matrix @ query.astype(np.float32)
            order = np.argsort(scores)[::-1][:top_k]
            return [(paths[idx], float(scores[idx])) for idx in order if idx < len(paths)]
        return []

    def _map_results(self, scores: Sequence[float], ids: Sequence[int]) -> List[Tuple[str, float]]:
        if self._meta_path.exists() and not self._paths:
            try:
                data = json.loads(self._meta_path.read_text(encoding="utf-8"))
                self._paths = list(data.get("paths", []))
            except Exception:
                self._paths = []
        results: List[Tuple[str, float]] = []
        for score, idx in zip(scores, ids):
            if idx is None:
                continue
            try:
                path = self._paths[int(idx)]
            except (IndexError, ValueError, TypeError):
                continue
            results.append((path, float(score)))
        return results


__all__ = ["ANNIndexManager"]
