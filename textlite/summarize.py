"""Summarization helpers for TextLite."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import List, Optional

LOGGER = logging.getLogger("videocatalog.textlite.summarize")

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9_']+")


@dataclass(slots=True)
class SummaryResult:
    summary: str
    keywords: List[str]


def _split_sentences(text: str) -> List[str]:
    sentences = _SENTENCE_RE.split(text)
    return [sentence.strip() for sentence in sentences if sentence.strip()]


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
        self._target_tokens = max(40, int(target_tokens))

    def summarize(self, text: str) -> str:
        chunk = text.strip()
        if not chunk:
            return ""
        max_length = min(512, self._target_tokens)
        min_length = max(20, min(160, max_length // 2))
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


def _textrank_like(sentences: List[str], *, max_tokens: int) -> str:
    if not sentences:
        return ""
    tokens = [_WORD_RE.findall(sentence.lower()) for sentence in sentences]
    freq: dict[str, int] = {}
    for sentence_tokens in tokens:
        for token in sentence_tokens:
            if len(token) < 3:
                continue
            freq[token] = freq.get(token, 0) + 1
    if not freq:
        return " ".join(sentences[:3])[: max_tokens * 6]
    scores = []
    for sentence, sentence_tokens in zip(sentences, tokens):
        weight = sum(freq.get(token, 0) for token in sentence_tokens)
        length_penalty = math.sqrt(len(sentence) + 1)
        score = weight / length_penalty if length_penalty else weight
        scores.append((score, sentence))
    scores.sort(key=lambda item: item[0], reverse=True)
    selected: List[str] = []
    total_chars = 0
    target_chars = max_tokens * 6
    for _score, sentence in scores:
        if sentence in selected:
            continue
        selected.append(sentence)
        total_chars += len(sentence)
        if total_chars >= target_chars or len(selected) >= 4:
            break
    if not selected:
        selected = sentences[:2]
    selected.sort(key=lambda s: sentences.index(s))
    return " ".join(selected)


def _keyword_candidates(text: str) -> List[str]:
    tokens = [tok.lower() for tok in _WORD_RE.findall(text)]
    freq: dict[str, int] = {}
    for token in tokens:
        if len(token) < 3:
            continue
        freq[token] = freq.get(token, 0) + 1
    return [token for token, _ in sorted(freq.items(), key=lambda item: (-item[1], item[0]))]


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
    def __init__(self, *, allow_gpu: bool, target_tokens: int, keywords_topk: int) -> None:
        self._target_tokens = max(40, int(target_tokens))
        self._keywords_topk = max(0, int(keywords_topk))
        self._transformer: Optional[TransformerSummarizer] = None
        if allow_gpu:
            use_gpu = True
        else:
            use_gpu = False
        try:
            self._transformer = TransformerSummarizer(use_gpu=use_gpu, target_tokens=self._target_tokens)
            LOGGER.info("Transformer summarizer initialised for TextLite (GPU=%s)", use_gpu)
        except Exception as exc:
            LOGGER.info("Falling back to heuristic summariser for TextLite: %s", exc)
            self._transformer = None

    def summarize(self, text: str) -> str:
        snippet = text.strip()
        if not snippet:
            return ""
        if self._transformer is not None:
            summary = self._transformer.summarize(snippet)
            if summary:
                return summary
        sentences = _split_sentences(snippet)
        if not sentences:
            return snippet[: self._target_tokens * 6]
        return _textrank_like(sentences, max_tokens=self._target_tokens)

    def keywords(self, text: str) -> List[str]:
        if self._keywords_topk <= 0:
            return []
        snippet = text.strip()
        if not snippet:
            return []
        yake_keywords = _try_yake(snippet, self._keywords_topk)
        if yake_keywords:
            return yake_keywords[: self._keywords_topk]
        return _keyword_candidates(snippet)[: self._keywords_topk]

    def run(self, text: str) -> SummaryResult:
        summary = self.summarize(text)
        keywords = self.keywords(text)
        return SummaryResult(summary=summary, keywords=keywords)


__all__ = ["Summarizer", "SummaryResult"]
