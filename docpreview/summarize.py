"""Summaries and keyword extraction for document previews."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

LOGGER = logging.getLogger("videocatalog.docpreview.summarize")

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9_']+")


@dataclass(slots=True)
class SummaryResult:
    summary: str
    keywords: List[str]


def _split_sentences(text: str, limit: int) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    cleaned = [s.strip() for s in sentences if s.strip()]
    return cleaned[:limit]


class TransformerSummarizer:
    def __init__(self, *, use_gpu: bool, target_tokens: int) -> None:
        try:
            from transformers import pipeline  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(f"transformers unavailable: {exc}")

        kwargs = {"model": "sshleifer/distilbart-cnn-12-6"}
        device = -1
        if use_gpu:
            try:
                import torch

                if torch.cuda.is_available():  # pragma: no cover - hardware specific
                    device = 0
            except Exception as exc:
                LOGGER.info("GPU summarizer unavailable: %s", exc)
        self._pipeline = pipeline("summarization", device=device, **kwargs)
        self._target_tokens = max(30, int(target_tokens))

    def summarize(self, text: str) -> str:
        chunk = text.strip()
        if not chunk:
            return ""
        max_length = min(512, self._target_tokens)
        min_length = max(20, min(120, max_length // 2))
        try:
            result = self._pipeline(
                chunk,
                max_length=max_length,
                min_length=min_length,
                truncation=True,
            )
        except Exception as exc:  # pragma: no cover - runtime dependent
            LOGGER.warning("Transformer summarizer failed: %s", exc)
            return ""
        if isinstance(result, list) and result:
            payload = result[0]
            summary = payload.get("summary_text") if isinstance(payload, dict) else None
            if isinstance(summary, str):
                return summary.strip()
        return ""


def _fallback_summary(text: str, target_tokens: int) -> str:
    sentences = _split_sentences(text, 5)
    if not sentences:
        return ""
    # Simple heuristic: use first N sentences up to desired length.
    target_chars = max(200, target_tokens * 6)
    summary_parts: List[str] = []
    total = 0
    for sent in sentences:
        summary_parts.append(sent)
        total += len(sent)
        if total >= target_chars:
            break
    return " ".join(summary_parts)


def _keyword_candidates(text: str) -> List[str]:
    tokens = [tok.lower() for tok in _WORD_RE.findall(text)]
    freq: dict[str, int] = {}
    for token in tokens:
        if len(token) < 3:
            continue
        freq[token] = freq.get(token, 0) + 1
    sorted_tokens = sorted(freq.items(), key=lambda item: (-item[1], item[0]))
    return [tok for tok, _ in sorted_tokens]


def _try_yake(text: str, topk: int) -> Optional[List[str]]:
    try:
        import yake  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        return None
    try:
        extractor = yake.KeywordExtractor(n=1, top=topk)
        keywords = extractor.extract_keywords(text)
    except Exception as exc:  # pragma: no cover - runtime dependent
        LOGGER.warning("YAKE keyword extraction failed: %s", exc)
        return None
    return [word for word, _ in keywords if isinstance(word, str)]


class Summarizer:
    def __init__(
        self,
        *,
        allow_gpu: bool,
        target_tokens: int,
    ) -> None:
        self._target_tokens = max(40, int(target_tokens))
        self._transformer: Optional[TransformerSummarizer] = None
        if allow_gpu:
            use_gpu = True
        else:
            use_gpu = False
        try:
            self._transformer = TransformerSummarizer(
                use_gpu=use_gpu,
                target_tokens=self._target_tokens,
            )
            LOGGER.info("Transformer summarizer initialised (GPU=%s)", use_gpu)
        except Exception as exc:
            LOGGER.info("Falling back to heuristic summariser: %s", exc)
            self._transformer = None

    def summarize(self, text: str) -> str:
        if not text.strip():
            return ""
        if self._transformer is not None:
            summary = self._transformer.summarize(text)
            if summary:
                return summary
        return _fallback_summary(text, self._target_tokens)

    def keywords(self, text: str, topk: int) -> List[str]:
        if topk <= 0:
            return []
        yake_keywords = _try_yake(text, topk)
        if yake_keywords:
            return yake_keywords[:topk]
        candidates = _keyword_candidates(text)
        return candidates[:topk]

    def run(self, text: str, *, topk: int) -> SummaryResult:
        summary = self.summarize(text)
        keywords = self.keywords(text, topk)
        return SummaryResult(summary=summary, keywords=keywords)


__all__ = ["Summarizer", "SummaryResult"]
