"""Document type detection and heuristics for scanned PDFs."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

LOGGER = logging.getLogger("videocatalog.docpreview.detect")

DocType = Literal["pdf", "epub", "unknown"]


@dataclass(slots=True)
class DetectionResult:
    """Summary of detection probes performed against a document."""

    doc_type: DocType
    is_scanned: bool = False
    page_count: Optional[int] = None
    notes: str = ""


def detect_doc_type(path: Path) -> DocType:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".epub":
        return "epub"
    # Attempt a simple MIME sniff from extension to keep things cheap.
    if suffix in {".xhtml", ".opf"}:
        return "epub"
    return "unknown"


def _pdf_text_density(path: Path, max_pages: int = 3) -> tuple[int, int]:
    """Return ``(chars, pages_checked)`` best-effort using PyMuPDF or pdfminer."""

    try:
        import fitz  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        fitz = None  # type: ignore

    if fitz is not None:
        try:
            doc = fitz.open(str(path))
        except Exception as exc:  # pragma: no cover - environment specific
            LOGGER.debug("PyMuPDF failed to open %s: %s", path, exc)
        else:
            with doc:
                page_total = doc.page_count
                pages = min(max_pages, page_total)
                total_chars = 0
                for index in range(pages):
                    try:
                        page = doc.load_page(index)
                        text = page.get_text("text")
                    except Exception as exc:  # pragma: no cover - best effort
                        LOGGER.debug("PyMuPDF get_text failed on %s page %d: %s", path, index, exc)
                        continue
                    total_chars += len(text.strip())
                return total_chars, pages

    # Fallback to pdfminer when PyMuPDF unavailable.
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        return 0, 0

    try:
        text = extract_text(str(path), maxpages=max_pages)
    except Exception as exc:  # pragma: no cover - best effort
        LOGGER.debug("pdfminer failed to read %s: %s", path, exc)
        return 0, 0
    chars = len(text.strip())
    return chars, min(max_pages, text.count("\f") + 1 if text else max_pages)


def detect_pdf(path: Path, *, sample_pages: int = 3) -> DetectionResult:
    """Return detection metadata for a PDF file."""

    total_chars, pages_checked = _pdf_text_density(path, max_pages=sample_pages)
    is_scanned = pages_checked > 0 and total_chars < max(60, pages_checked * 30)
    notes = (
        f"chars={total_chars} pages_sampled={pages_checked}"
        if pages_checked
        else "density_unavailable"
    )
    page_count: Optional[int] = None
    try:
        import fitz  # type: ignore
    except Exception:  # pragma: no cover
        fitz = None  # type: ignore
    if fitz is not None:
        try:
            doc = fitz.open(str(path))
        except Exception:
            page_count = None
        else:
            with doc:
                page_count = int(getattr(doc, "page_count", 0) or 0)
    return DetectionResult(doc_type="pdf", is_scanned=is_scanned, page_count=page_count, notes=notes)


def probe(path: Path) -> DetectionResult:
    """Detect the document type and key heuristics for *path*."""

    doc_type = detect_doc_type(path)
    if doc_type == "pdf":
        return detect_pdf(path)
    if doc_type == "epub":
        return DetectionResult(doc_type="epub", is_scanned=False, page_count=None, notes="epub")
    return DetectionResult(doc_type="unknown", is_scanned=False, page_count=None, notes="unknown")


__all__ = ["DetectionResult", "DocType", "detect_doc_type", "detect_pdf", "probe"]
