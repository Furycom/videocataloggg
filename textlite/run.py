"""Batch orchestration for TextLite previews."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.db import connect
from robust import CancellationToken, to_fs_path

from . import detect, extract, sample
from .store import PreviewRow, PreviewStore, ensure_tables
from .summarize import Summarizer

LOGGER = logging.getLogger("videocatalog.textlite")

ProgressCallback = Callable[[Dict[str, object]], None]


@dataclass(slots=True)
class TextLiteSettings:
    enable: bool = True
    max_bytes: int = 32768
    max_lines: int = 400
    head_lines: int = 150
    mid_lines: int = 100
    tail_lines: int = 150
    summary_tokens: int = 80
    keywords_topk: int = 8
    schema_csv_headers: bool = True
    schema_json_keys: bool = True
    schema_yaml_keys: bool = True
    skip_if_gt_mb: int = 50
    gentle_sleep_ms: int = 2
    gpu_allowed: bool = True

    @classmethod
    def from_mapping(cls, mapping: Dict[str, object] | None) -> "TextLiteSettings":
        data = dict(mapping or {})
        schema_cfg = data.get("schema") if isinstance(data.get("schema"), dict) else {}
        return cls(
            enable=bool(data.get("enable", True)),
            max_bytes=max(4096, int(data.get("max_bytes", 32768) or 32768)),
            max_lines=max(40, int(data.get("max_lines", 400) or 400)),
            head_lines=max(10, int(data.get("head_lines", 150) or 150)),
            mid_lines=max(0, int(data.get("mid_lines", 100) or 100)),
            tail_lines=max(10, int(data.get("tail_lines", 150) or 150)),
            summary_tokens=max(40, int(data.get("summary_tokens", 80) or 80)),
            keywords_topk=max(0, int(data.get("keywords_topk", 8) or 8)),
            schema_csv_headers=bool(schema_cfg.get("csv_headers", True)),
            schema_json_keys=bool(schema_cfg.get("json_keys", True)),
            schema_yaml_keys=bool(schema_cfg.get("yaml_keys", True)),
            skip_if_gt_mb=max(1, int(data.get("skip_if_gt_mb", 50) or 50)),
            gentle_sleep_ms=max(0, int(data.get("gentle_sleep_ms", 2) or 2)),
            gpu_allowed=bool(data.get("gpu_allowed", True)),
        )

    def with_overrides(self, **kwargs: object) -> "TextLiteSettings":
        payload = asdict(self)
        payload.update(kwargs)
        schema_payload = {
            "csv_headers": self.schema_csv_headers,
            "json_keys": self.schema_json_keys,
            "yaml_keys": self.schema_yaml_keys,
        }
        if "schema" not in payload:
            payload["schema"] = schema_payload
        payload.update(kwargs)
        return TextLiteSettings.from_mapping(payload)

    def schema_budget(self) -> Dict[str, bool]:
        return {
            "csv_headers": self.schema_csv_headers,
            "json_keys": self.schema_json_keys,
            "yaml_keys": self.schema_yaml_keys,
        }


@dataclass(slots=True)
class TextLiteSummary:
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    updated: int = 0
    elapsed_s: float = 0.0


class TextLiteRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: TextLiteSettings,
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
            target_tokens=self.settings.summary_tokens,
            keywords_topk=self.settings.keywords_topk,
        )
        self.sample_config = sample.SampleConfig(
            max_bytes=self.settings.max_bytes,
            max_lines=self.settings.max_lines,
            head_lines=self.settings.head_lines,
            mid_lines=self.settings.mid_lines,
            tail_lines=self.settings.tail_lines,
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

    def _fs_path(self, path: str) -> str:
        if os.name == "nt":
            return to_fs_path(path, mode=self.long_path_mode)
        return path

    def _candidates(self, limit: Optional[int] = None) -> List[Dict[str, object]]:
        skip_bytes = self.settings.skip_if_gt_mb * 1024 * 1024
        extensions = sorted(ext.strip(".").lower() for ext in detect.supported_extensions())
        placeholders = ",".join("?" for _ in extensions)
        sql = f"""
            SELECT inv.path, inv.ext, inv.mime, inv.size_bytes, inv.indexed_utc
            FROM inventory AS inv
            LEFT JOIN textlite_preview AS prev ON prev.path = inv.path
            WHERE LOWER(COALESCE(inv.ext,'')) IN ({placeholders})
              AND inv.size_bytes <= ?
              AND (prev.updated_utc IS NULL OR prev.updated_utc < inv.indexed_utc)
            ORDER BY inv.indexed_utc DESC
        """
        params: List[object] = list(extensions)
        params.append(skip_bytes)
        cur = self.conn.cursor()
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = cur.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def run(self, *, limit: Optional[int] = None) -> TextLiteSummary:
        start = time.perf_counter()
        summary = TextLiteSummary()
        candidates = self._candidates(limit=limit)
        total = len(candidates)
        LOGGER.info("TextLite processing %d candidates", total)
        self._emit(
            {
                "type": "textlite",
                "total": total,
                "processed": 0,
                "skipped": 0,
                "errors": 0,
                "eta_s": None,
            }
        )
        skip_bytes = self.settings.skip_if_gt_mb * 1024 * 1024
        for index, row in enumerate(candidates, start=1):
            if self._should_cancel():
                LOGGER.info("TextLite cancelled after %d items", summary.processed)
                break
            inv_path = row.get("path")
            if not isinstance(inv_path, str):
                summary.skipped += 1
                continue
            fs_path = self._fs_path(inv_path)
            text_path = Path(fs_path)
            try:
                probe = detect.probe(
                    text_path,
                    ext=str(row.get("ext", "")),
                    mime=row.get("mime"),
                    size_bytes=row.get("size_bytes"),
                    skip_if_gt_bytes=skip_bytes,
                )
            except Exception as exc:
                LOGGER.warning("TextLite detect failed for %s: %s", inv_path, exc)
                summary.errors += 1
                continue
            if probe is None or (probe.skipped and probe.reason):
                summary.skipped += 1
                continue
            if probe.skipped:
                summary.skipped += 1
                continue
            try:
                snippet = sample.sample_text(text_path, self.sample_config)
            except Exception as exc:
                LOGGER.warning("TextLite sampling failed for %s: %s", inv_path, exc)
                summary.errors += 1
                continue
            text = snippet.text.strip()
            if not text:
                summary.skipped += 1
                continue
            try:
                summary_result = self.summarizer.run(text)
            except Exception as exc:
                LOGGER.warning("TextLite summarization failed for %s: %s", inv_path, exc)
                summary.errors += 1
                continue
            schema_payload: Optional[Dict[str, List[str]]] = None
            try:
                hints = extract.build_schema(
                    text_path,
                    probe.kind,
                    max_bytes=self.settings.max_bytes,
                )
                filtered: Dict[str, List[str]] = {}
                schema_flags = self.settings.schema_budget()
                data = hints.as_dict()
                for key, enabled in schema_flags.items():
                    if enabled and key in data and data[key]:
                        filtered[key] = data[key]
                if filtered:
                    schema_payload = filtered
            except Exception as exc:
                LOGGER.debug("TextLite schema extraction failed for %s: %s", inv_path, exc)
            preview = PreviewRow(
                path=inv_path,
                kind=probe.kind,
                bytes_sampled=snippet.bytes_sampled,
                lines_sampled=snippet.lines_sampled,
                summary=summary_result.summary,
                keywords=summary_result.keywords,
                schema_json=json.dumps(schema_payload, ensure_ascii=False) if schema_payload else None,
            )
            self.store.add(preview)
            summary.processed += 1
            summary.updated += 1
            elapsed = time.perf_counter() - start
            remaining = total - index
            eta = (elapsed / index) * remaining if index else None
            payload = {
                "type": "textlite",
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
        self._emit(
            {
                "type": "textlite",
                "total": total,
                "processed": summary.processed,
                "skipped": summary.skipped,
                "errors": summary.errors,
                "eta_s": 0,
                "done": True,
            }
        )
        return summary


def run_for_shard(
    shard_path: Path,
    *,
    settings: TextLiteSettings,
    gpu_settings: Optional[Dict[str, object]] = None,
    progress_callback: Optional[ProgressCallback] = None,
    cancellation: Optional[CancellationToken] = None,
    gentle_sleep: float = 0.0,
    limit: Optional[int] = None,
) -> TextLiteSummary:
    conn = connect(shard_path, read_only=False, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        runner = TextLiteRunner(
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
    "TextLiteRunner",
    "TextLiteSettings",
    "TextLiteSummary",
    "run_for_shard",
]
