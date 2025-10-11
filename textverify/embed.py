"""Sentence embedding helpers for plot cross-check."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

import numpy as np

LOGGER = logging.getLogger("videocatalog.textverify.embed")


@dataclass(slots=True)
class EmbeddingResult:
    vector: np.ndarray


class SentenceEmbedder:
    """Best-effort embedding loader with CPU/GPU awareness."""

    def __init__(self, model_name: str, *, allow_gpu: bool) -> None:
        self._model_name = model_name
        self._allow_gpu = allow_gpu
        self._model = None
        self._token_pattern = re.compile(r"[\wÀ-ÖØ-öø-ÿ']+")
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            device = "cpu"
            if allow_gpu:
                try:
                    import torch

                    if torch.cuda.is_available():  # pragma: no cover - hardware specific
                        device = "cuda"
                except Exception as exc:
                    LOGGER.info("Unable to use GPU for embeddings: %s", exc)
            self._model = SentenceTransformer(model_name, device=device)
            LOGGER.info("SentenceTransformer loaded (%s, device=%s)", model_name, device)
        except Exception as exc:
            LOGGER.info("Falling back to hashing embeddings (%s)", exc)
            self._model = None

    def encode(self, text: str) -> EmbeddingResult:
        cleaned = text.strip()
        if not cleaned:
            return EmbeddingResult(vector=self._zero_vector())
        if self._model is not None:
            try:
                vector = self._model.encode(cleaned, convert_to_numpy=True)
            except Exception as exc:
                LOGGER.warning("Embedding model failed: %s", exc)
                vector = self._fallback_vector(cleaned)
        else:
            vector = self._fallback_vector(cleaned)
        if not isinstance(vector, np.ndarray):
            vector = np.asarray(vector, dtype=np.float32)
        vector = vector.astype(np.float32, copy=False)
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        return EmbeddingResult(vector=vector)

    def _zero_vector(self) -> np.ndarray:
        return np.zeros(256, dtype=np.float32)

    def _fallback_vector(self, text: str) -> np.ndarray:
        tokens = [token.lower() for token in self._token_pattern.findall(text)]
        dim = 256
        vector = np.zeros(dim, dtype=np.float32)
        if not tokens:
            return vector
        for token in tokens:
            idx = hash(token) % dim
            vector[idx] += 1.0
        total = math.sqrt(float((vector ** 2).sum()))
        if total > 0:
            vector /= total
        return vector


__all__ = ["SentenceEmbedder", "EmbeddingResult"]
