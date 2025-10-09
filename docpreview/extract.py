"""Lightweight text extraction helpers for document previews."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

LOGGER = logging.getLogger("videocatalog.docpreview.extract")


@dataclass(slots=True)
class ExtractedText:
    text: str
    pages_sampled: int
    chars: int


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []

    def handle_data(self, data: str) -> None:  # pragma: no cover - trivial
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def get_text(self) -> str:
        return " \n".join(self._parts)


def _truncate(text: str, limit: int) -> tuple[str, int]:
    if limit <= 0:
        return "", 0
    if len(text) <= limit:
        return text, len(text)
    return text[:limit], limit


def choose_pdf_pages(page_count: Optional[int], max_pages: int, strategy: str = "smart") -> List[int]:
    if not page_count or page_count <= 0:
        return list(range(max_pages))
    max_pages = max(1, min(max_pages, page_count))
    if strategy.lower() == "first":
        return list(range(max_pages))
    picks = {0}
    if page_count > 1:
        picks.add(1)
        picks.add(page_count - 1)
    if page_count > 2:
        picks.add(page_count // 2)
    if page_count > 4:
        picks.add(page_count // 3)
        picks.add((2 * page_count) // 3)
    ordered = sorted(picks)
    if len(ordered) >= max_pages:
        return ordered[:max_pages]
    for idx in range(page_count):
        if len(ordered) >= max_pages:
            break
        if idx not in picks:
            ordered.append(idx)
    return ordered[:max_pages]


def _extract_pdf_pymupdf(path: Path, pages: Sequence[int], max_chars: int) -> ExtractedText:
    import fitz  # type: ignore

    text_parts: List[str] = []
    sampled = 0
    try:
        doc = fitz.open(str(path))
    except Exception as exc:  # pragma: no cover - environment specific
        raise RuntimeError(f"open failed: {exc}")
    with doc:
        count = doc.page_count
        for index in pages:
            if index < 0 or index >= count:
                continue
            try:
                page = doc.load_page(index)
                text = page.get_text("text")
            except Exception as exc:  # pragma: no cover - best effort
                LOGGER.debug("PyMuPDF get_text error on %s page %d: %s", path, index, exc)
                continue
            stripped = text.strip()
            if not stripped:
                continue
            text_parts.append(stripped)
            sampled += 1
            if sum(len(part) for part in text_parts) >= max_chars:
                break
    joined = "\n\n".join(text_parts)
    truncated, used = _truncate(joined, max_chars)
    return ExtractedText(truncated, sampled, used)


def _extract_pdf_pdfminer(path: Path, pages: Sequence[int], max_chars: int) -> ExtractedText:
    from pdfminer.high_level import extract_text_to_fp  # type: ignore

    output = BytesIO()
    page_numbers = sorted(set(idx for idx in pages if idx >= 0))
    if not page_numbers:
        page_numbers = list(range(len(pages) or 1))
    try:
        extract_text_to_fp(  # type: ignore
            str(path),
            output,
            page_numbers=page_numbers,
            maxpages=len(page_numbers),
            output_type="text",
            codec="utf-8",
        )
    except Exception as exc:  # pragma: no cover - best effort
        raise RuntimeError(f"pdfminer failed: {exc}")
    text = output.getvalue().decode("utf-8", errors="ignore")
    truncated, used = _truncate(text, max_chars)
    return ExtractedText(truncated, len(page_numbers), used)


def extract_pdf_text(path: Path, pages: Sequence[int], max_chars: int) -> ExtractedText:
    try:
        import fitz  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        fitz = None  # type: ignore
    if fitz is not None:
        try:
            return _extract_pdf_pymupdf(path, pages, max_chars)
        except Exception as exc:
            LOGGER.debug("PyMuPDF extraction failed for %s: %s", path, exc)
    try:
        return _extract_pdf_pdfminer(path, pages, max_chars)
    except Exception as exc:
        LOGGER.warning("PDF extraction failed for %s: %s", path, exc)
        return ExtractedText("", 0, 0)


def extract_epub_text(path: Path, max_chars: int) -> ExtractedText:
    try:
        from ebooklib import epub  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        LOGGER.warning("EbookLib not available: %s", exc)
        return ExtractedText("", 0, 0)

    try:
        book = epub.read_epub(str(path))  # type: ignore
    except Exception as exc:
        LOGGER.warning("Failed to read EPUB %s: %s", path, exc)
        return ExtractedText("", 0, 0)

    parts: List[str] = []
    sampled = 0
    for item in book.get_items_of_type(epub.ITEM_DOCUMENT):  # type: ignore[attr-defined]
        try:
            raw = item.get_content()
        except Exception:
            continue
        try:
            decoded = raw.decode("utf-8", errors="ignore")
        except Exception:
            decoded = raw.decode(errors="ignore")
        parser = _HTMLToText()
        try:
            parser.feed(decoded)
        except Exception:
            continue
        chunk = parser.get_text()
        if not chunk.strip():
            continue
        parts.append(chunk)
        sampled += 1
        if sum(len(part) for part in parts) >= max_chars:
            break
    joined = "\n\n".join(parts)
    truncated, used = _truncate(joined, max_chars)
    return ExtractedText(truncated, sampled, used)


__all__ = [
    "ExtractedText",
    "choose_pdf_pages",
    "extract_epub_text",
    "extract_pdf_text",
]
