"""Batch orchestration for document previews."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.db import connect
from robust import CancellationToken, to_fs_path

from . import detect
from .extract import choose_pdf_pages, extract_epub_text, extract_pdf_text
from .ocr import run_ocr
from .store import DiagRow, PreviewRow, PreviewStore, ensure_tables
from .summarize import Summarizer

LOGGER = logging.getLogger("videocatalog.docpreview")

ProgressCallback = Callable[[Dict[str, object]], None]


@dataclass(slots=True)
class DocPreviewSettings:
    enable: bool = False
    max_pages: int = 6
    max_chars: int = 20000
    sample_strategy: str = "smart"
    ocr_enable: bool = True
    ocr_max_pages: int = 2
    ocr_timeout_s: float = 20.0
    summary_target_tokens: int = 120
    keywords_topk: int = 10
    gpu_allowed: bool = True

    @classmethod
    def from_mapping(cls, mapping: Dict[str, object] | None) -> "DocPreviewSettings":
        data = dict(mapping or {})
        return cls(
            enable=bool(data.get("enable", False)),
            max_pages=max(1, int(data.get("max_pages", 6) or 6)),
            max_chars=max(1000, int(data.get("max_chars", 20000) or 20000)),
            sample_strategy=str(data.get("sample_strategy", "smart") or "smart"),
            ocr_enable=bool(data.get("ocr_enable", True)),
            ocr_max_pages=max(0, int(data.get("ocr_max_pages", 2) or 2)),
            ocr_timeout_s=max(1.0, float(data.get("ocr_timeout_s", 20.0) or 20.0)),
            summary_target_tokens=max(40, int(data.get("summary_target_tokens", 120) or 120)),
            keywords_topk=max(0, int(data.get("keywords_topk", 10) or 10)),
            gpu_allowed=bool(data.get("gpu_allowed", True)),
        )

    def with_overrides(self, **kwargs: object) -> "DocPreviewSettings":
        payload = asdict(self)
        payload.update(kwargs)
        return DocPreviewSettings.from_mapping(payload)

    def normalized_strategy(self) -> str:
        value = (self.sample_strategy or "smart").lower()
        if value not in {"smart", "first"}:
            value = "smart"
        return value


@dataclass(slots=True)
class DocPreviewSummary:
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    updated: int = 0
    elapsed_s: float = 0.0


class DocPreviewRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: DocPreviewSettings,
        gpu_settings: Optional[Dict[str, object]] = None,
        progress_callback: Optional[ProgressCallback] = None,
        cancellation: Optional[CancellationToken] = None,
        gentle_sleep: float = 0.0,
        long_path_mode: str = "auto",
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.gpu_settings = dict(gpu_settings or {})
        self.progress_callback = progress_callback
        self.cancellation = cancellation
        self.gentle_sleep = max(0.0, float(gentle_sleep))
        self.long_path_mode = long_path_mode
        self._last_progress_emit = 0.0
        ensure_tables(self.conn)
        self.store = PreviewStore(self.conn)
        allow_gpu = self._resolve_gpu_policy()
        self.summarizer = Summarizer(
            allow_gpu=allow_gpu and self.settings.gpu_allowed,
            target_tokens=self.settings.summary_target_tokens,
        )

    def _resolve_gpu_policy(self) -> bool:
        policy = str(self.gpu_settings.get("policy", "AUTO") or "AUTO").upper()
        if policy == "CPU_ONLY":
            return False
        return True

    def _emit(self, payload: Dict[str, object]) -> None:
        self._last_progress_emit = time.monotonic()
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(payload)
        except Exception:
            pass

    def _should_cancel(self) -> bool:
        if self.cancellation is None:
            return False
        try:
            if hasattr(self.cancellation, "is_cancelled"):
                return bool(self.cancellation.is_cancelled())  # type: ignore[attr-defined]
            return bool(self.cancellation.is_set())
        except Exception:
            return False

    def _fts_candidates(self, limit: Optional[int] = None) -> List[Dict[str, object]]:
        cur = self.conn.cursor()
        sql = """
            SELECT inv.path, inv.ext, inv.mime, inv.indexed_utc, inv.mtime_utc
            FROM inventory AS inv
            LEFT JOIN docs_preview AS prev ON prev.path = inv.path
            WHERE (
                LOWER(COALESCE(inv.ext,'')) IN ('pdf','epub')
                OR LOWER(COALESCE(inv.mime,'')) IN ('application/pdf','application/epub+zip','application/x-epub+zip')
            )
            AND (prev.updated_utc IS NULL OR prev.updated_utc < inv.indexed_utc)
            ORDER BY inv.indexed_utc DESC
        """
        if limit is not None:
            sql += " LIMIT ?"
            rows = cur.execute(sql, (int(limit),)).fetchall()
        else:
            rows = cur.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def _fs_path(self, path: str) -> str:
        if os.name == "nt":
            return to_fs_path(path, mode=self.long_path_mode)
        return path

    def _guess_language(self, text: str) -> Optional[str]:
        snippet = text[:4000]
        if not snippet.strip():
            return None
        try:
            from langdetect import detect_langs  # type: ignore
        except Exception:  # pragma: no cover - optional dependency
            return None
        try:
            guesses = detect_langs(snippet)
        except Exception:  # pragma: no cover - runtime dependent
            return None
        if not guesses:
            return None
        top = guesses[0]
        if getattr(top, "prob", 0.0) >= 0.6:
            return getattr(top, "lang", None)
        return None

    def _summarize(self, text: str) -> tuple[str, List[str]]:
        result = self.summarizer.run(text, topk=self.settings.keywords_topk)
        keywords = [kw.strip() for kw in result.keywords if kw.strip()]
        return result.summary.strip(), keywords

    def _process_pdf(self, path: Path, info: detect.DetectionResult) -> tuple[str, int, str, bool]:
        pages = choose_pdf_pages(info.page_count, self.settings.max_pages, self.settings.normalized_strategy())
        extracted = extract_pdf_text(path, pages, self.settings.max_chars)
        text = extracted.text
        had_ocr = False
        ocr_result: Optional[OCRResult] = None
        notes = []
        if not text.strip() and self.settings.ocr_enable and info.is_scanned and self.settings.ocr_max_pages > 0:
            ocr_pages = pages[: self.settings.ocr_max_pages]
            ocr_result = run_ocr(
                path,
                ocr_pages,
                max_pages=self.settings.ocr_max_pages,
                timeout_s=self.settings.ocr_timeout_s,
            )
            if ocr_result.text.strip():
                text = ocr_result.text
                had_ocr = True
            notes.append(f"ocr={ocr_result.notes}")
        pages_sampled = extracted.pages_sampled or len(pages)
        if ocr_result is not None and ocr_result.pages_processed:
            pages_sampled = max(pages_sampled, ocr_result.pages_processed)
            notes.append(f"ocr_pages={ocr_result.pages_processed}")
        notes.append(f"pages={pages_sampled}")
        return text, pages_sampled, ";".join(notes), had_ocr

    def _process_epub(self, path: Path) -> tuple[str, int, str]:
        extracted = extract_epub_text(path, self.settings.max_chars)
        return extracted.text, extracted.pages_sampled, "epub"

    def run(self, *, limit: Optional[int] = None) -> DocPreviewSummary:
        start = time.perf_counter()
        summary = DocPreviewSummary()
        candidates = self._fts_candidates(limit=limit)
        total = len(candidates)
        LOGGER.info("Doc preview processing %d candidates", total)
        self._emit(
            {
                "type": "docpreview",
                "total": total,
                "processed": 0,
                "skipped": 0,
                "errors": 0,
                "eta_s": None,
            }
        )
        for index, row in enumerate(candidates, start=1):
            if self._should_cancel():
                LOGGER.info("Doc preview cancelled after %d items", summary.processed)
                break
            inv_path = row.get("path")
            if not isinstance(inv_path, str):
                summary.skipped += 1
                continue
            fs_path = self._fs_path(inv_path)
            doc_path = Path(fs_path)
            try:
                probe = detect.probe(doc_path)
            except Exception as exc:
                LOGGER.warning("Doc preview probe failed for %s: %s", inv_path, exc)
                summary.errors += 1
                continue
            if probe.doc_type not in {"pdf", "epub"}:
                summary.skipped += 1
                continue
            text = ""
            diag_notes = [probe.notes] if probe.notes else []
            pages_sampled = 0
            had_ocr = False
            extracted: Optional[ExtractedText] = None
            ocr_result: Optional[OCRResult] = None
            try:
                if probe.doc_type == "pdf":
                    text, pages_sampled, note, had_ocr = self._process_pdf(doc_path, probe)
                    diag_notes.append(note)
                elif probe.doc_type == "epub":
                    text, pages_sampled, note = self._process_epub(doc_path)
                    diag_notes.append(note)
            except Exception as exc:
                LOGGER.warning("Doc preview extraction failed for %s: %s", inv_path, exc)
                summary.errors += 1
                continue
            trimmed = text.strip()
            if len(trimmed) > self.settings.max_chars:
                trimmed = trimmed[: self.settings.max_chars]
            if not trimmed:
                summary.skipped += 1
                diag_notes.append("empty")
                diag = DiagRow(
                    path=inv_path,
                    is_scanned=probe.is_scanned,
                    had_ocr=had_ocr,
                    notes=";".join(diag_notes),
                )
                self.store.add(
                    PreviewRow(
                        path=inv_path,
                        doc_type=probe.doc_type,
                        lang=None,
                        pages_sampled=pages_sampled,
                        chars_used=0,
                        summary="",
                        keywords=[],
                    ),
                    diag,
                )
                continue
            lang = self._guess_language(trimmed)
            summary_text, keywords = self._summarize(trimmed)
            preview = PreviewRow(
                path=inv_path,
                doc_type=probe.doc_type,
                lang=lang,
                pages_sampled=pages_sampled,
                chars_used=len(trimmed),
                summary=summary_text,
                keywords=keywords,
            )
            diag = DiagRow(
                path=inv_path,
                is_scanned=probe.is_scanned,
                had_ocr=had_ocr,
                notes=";".join(diag_notes),
            )
            self.store.add(preview, diag)
            summary.processed += 1
            summary.updated += 1
            elapsed = time.perf_counter() - start
            remaining = total - index
            eta = (elapsed / index) * remaining if index else None
            payload = {
                "type": "docpreview",
                "total": total,
                "processed": summary.processed,
                "skipped": summary.skipped,
                "errors": summary.errors,
                "eta_s": eta,
                "path": inv_path,
            }
            self._emit(payload)
            if self.gentle_sleep:
                time.sleep(self.gentle_sleep)
        self.store.flush()
        summary.elapsed_s = time.perf_counter() - start
        self._emit({
            "type": "docpreview",
            "total": total,
            "processed": summary.processed,
            "skipped": summary.skipped,
            "errors": summary.errors,
            "eta_s": 0,
            "done": True,
        })
        return summary


def run_for_shard(
    shard_path: Path,
    *,
    settings: DocPreviewSettings,
    gpu_settings: Optional[Dict[str, object]] = None,
    progress_callback: Optional[ProgressCallback] = None,
    cancellation: Optional[CancellationToken] = None,
    gentle_sleep: float = 0.0,
    limit: Optional[int] = None,
) -> DocPreviewSummary:
    conn = connect(shard_path, read_only=False, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        runner = DocPreviewRunner(
            conn,
            settings=settings,
            gpu_settings=gpu_settings,
            progress_callback=progress_callback,
            cancellation=cancellation,
            gentle_sleep=gentle_sleep,
        )
        return runner.run(limit=limit)
    finally:
        conn.close()


__all__ = [
    "DocPreviewRunner",
    "DocPreviewSettings",
    "DocPreviewSummary",
    "run_for_shard",
]
