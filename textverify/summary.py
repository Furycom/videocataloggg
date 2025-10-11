"""Summarisation and keyword extraction for subtitle snippets."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

LOGGER = logging.getLogger("videocatalog.textverify.summary")

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[\wÀ-ÖØ-öø-ÿ']+")


@dataclass(slots=True)
class SummaryResult:
    summary: str
    keywords: List[str]


class TransformerSummarizer:
    """Wrapper around Hugging Face transformers summarisation pipeline."""

    def __init__(self, model_name: str, *, use_gpu: bool, target_tokens: int) -> None:
        try:
            from transformers import pipeline  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(f"transformers unavailable: {exc}")
        device = -1
        if use_gpu:
            try:
                import torch

                if torch.cuda.is_available():  # pragma: no cover - hardware specific
                    device = 0
            except Exception as exc:
                LOGGER.info("Unable to use GPU for summarisation: %s", exc)
        kwargs = {"model": model_name}
        self._pipeline = pipeline("summarization", device=device, **kwargs)
        self._target_tokens = max(40, int(target_tokens))

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
            LOGGER.warning("Transformer summariser failed: %s", exc)
            return ""
        if isinstance(result, list) and result:
            payload = result[0]
            if isinstance(payload, dict):
                summary = payload.get("summary_text")
            else:
                summary = None
            if isinstance(summary, str):
                return summary.strip()
        return ""


def _fallback_summary(text: str, target_tokens: int) -> str:
    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    if not sentences:
        return text[: target_tokens * 6]
    target_chars = max(200, target_tokens * 6)
    picked: List[str] = []
    total = 0
    for sentence in sentences:
        picked.append(sentence)
        total += len(sentence)
        if total >= target_chars:
            break
    return " ".join(picked)


def _keyword_candidates(text: str) -> List[str]:
    tokens = [token.lower() for token in _WORD_RE.findall(text)]
    freq: dict[str, int] = {}
    for token in tokens:
        if len(token) < 3:
            continue
        freq[token] = freq.get(token, 0) + 1
    sorted_tokens = sorted(freq.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in sorted_tokens]


def _try_yake(text: str, topk: int) -> Optional[List[str]]:
    try:
        import yake  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        return None
    try:
        extractor = yake.KeywordExtractor(n=1, top=topk)
        keywords = extractor.extract_keywords(text)
    except Exception as exc:  # pragma: no cover - runtime dependent
        LOGGER.warning("YAKE extraction failed: %s", exc)
        return None
    return [word for word, _ in keywords if isinstance(word, str)]


class SummaryEngine:
    """High level helper that prefers transformers but falls back gracefully."""

    def __init__(
        self,
        *,
        model_name: str,
        target_tokens: int,
        allow_gpu: bool,
    ) -> None:
        self._target_tokens = max(40, int(target_tokens))
        self._transformer: Optional[TransformerSummarizer] = None
        try:
            self._transformer = TransformerSummarizer(
                model_name,
                use_gpu=allow_gpu,
                target_tokens=self._target_tokens,
            )
            LOGGER.info("Transformer summariser loaded (%s)", model_name)
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
        if topk <= 0 or not text.strip():
            return []
        yake_keywords = _try_yake(text, topk)
        if yake_keywords:
            return yake_keywords[:topk]
        return _keyword_candidates(text)[:topk]

    def run(self, text: str, *, topk: int) -> SummaryResult:
        summary = self.summarize(text)
        keywords = self.keywords(summary or text, topk)
        return SummaryResult(summary=summary, keywords=keywords)


__all__ = ["SummaryEngine", "SummaryResult"]
