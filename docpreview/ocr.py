"""Optional OCR helpers for scanned PDF pages."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Sequence

LOGGER = logging.getLogger("videocatalog.docpreview.ocr")


@dataclass(slots=True)
class OCRResult:
    text: str
    pages_processed: int
    notes: str = ""


def run_ocr(
    path: Path,
    pages: Sequence[int],
    *,
    max_pages: int,
    timeout_s: float,
    dpi: int = 200,
) -> OCRResult:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        LOGGER.info("OCR skipped: PyMuPDF unavailable (%s)", exc)
        return OCRResult("", 0, "pymupdf_missing")
    try:
        import pytesseract  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        LOGGER.info("OCR skipped: pytesseract unavailable (%s)", exc)
        return OCRResult("", 0, "pytesseract_missing")
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - optional dependency
        LOGGER.info("OCR skipped: Pillow unavailable (%s)", exc)
        return OCRResult("", 0, "pillow_missing")

    start = time.perf_counter()
    text_fragments: List[str] = []
    processed = 0
    deadline = start + float(timeout_s)
    with fitz.open(str(path)) as doc:
        for index in pages[:max_pages]:
            if time.perf_counter() >= deadline:
                LOGGER.info("OCR timeout reached for %s", path)
                break
            if index < 0 or index >= doc.page_count:
                continue
            try:
                page = doc.load_page(index)
                matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
                pixmap = page.get_pixmap(matrix=matrix)
                buffer = BytesIO(pixmap.tobytes("png"))
                image = Image.open(buffer)
                text = pytesseract.image_to_string(image)
            except Exception as exc:  # pragma: no cover - best effort
                LOGGER.debug("OCR failed for %s page %d: %s", path, index, exc)
                continue
            stripped = text.strip()
            if stripped:
                text_fragments.append(stripped)
            processed += 1
    joined = "\n\n".join(text_fragments)
    notes = "timeout" if time.perf_counter() >= deadline else "ok"
    return OCRResult(joined, processed, notes)


__all__ = ["OCRResult", "run_ocr"]
