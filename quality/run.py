"""Orchestration for the video quality report pipeline."""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.db import connect
from robust import CancellationToken, to_fs_path

from .ffprobe import ProbeResult, ffprobe_available, run_ffprobe
from .score import (
    QualityInput,
    QualityLabels,
    QualityResult,
    QualityThresholds,
    reasons_to_json,
    score_quality,
)
from .store import QualityRow, QualityStore

LOGGER = logging.getLogger("videocatalog.quality")

ProgressCallback = Callable[[Dict[str, object]], None]


@dataclass(slots=True)
class QualitySettings:
    enable: bool = True
    timeout_s: float = 8.0
    gentle_sleep_ms: int = 3
    max_parallel: int = 1
    thresholds: QualityThresholds = field(default_factory=QualityThresholds)
    labels: QualityLabels = field(default_factory=QualityLabels)

    @classmethod
    def from_mapping(cls, mapping: Dict[str, object] | None) -> "QualitySettings":
        data = dict(mapping or {})
        thresholds = QualityThresholds.from_mapping(
            data.get("thresholds") if isinstance(data.get("thresholds"), dict) else {}
        )
        labels = QualityLabels.from_mapping(
            data.get("labels") if isinstance(data.get("labels"), dict) else {}
        )
        return cls(
            enable=bool(data.get("enable", True)),
            timeout_s=float(data.get("timeout_s", 8) or 8),
            gentle_sleep_ms=int(data.get("gentle_sleep_ms", 3) or 3),
            max_parallel=int(data.get("max_parallel", 1) or 1),
            thresholds=thresholds,
            labels=labels,
        )

    def with_overrides(self, **kwargs: object) -> "QualitySettings":
        payload = asdict(self)
        payload.update(kwargs)
        payload["thresholds"] = asdict(self.thresholds)
        payload["labels"] = asdict(self.labels)
        return QualitySettings.from_mapping(payload)


@dataclass(slots=True)
class QualitySummary:
    processed: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    elapsed_s: float = 0.0
    ffprobe_missing: bool = False


class QualityRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: QualitySettings,
        mount_path: Optional[Path] = None,
        progress_callback: Optional[ProgressCallback] = None,
        cancellation: Optional[CancellationToken] = None,
        gentle_sleep: float = 0.0,
        long_path_mode: str = "auto",
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.mount_path = mount_path
        self.progress_callback = progress_callback
        self.cancellation = cancellation
        self.gentle_sleep = max(0.0, float(gentle_sleep))
        self.long_path_mode = long_path_mode
        self.store = QualityStore(self.conn)
        self.conn.row_factory = sqlite3.Row

    def _emit(self, payload: Dict[str, object]) -> None:
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

    def _inventory_candidates(self, *, limit: Optional[int] = None) -> List[Dict[str, object]]:
        sql = (
            """
            SELECT inv.path, inv.drive_label, inv.indexed_utc
            FROM inventory AS inv
            LEFT JOIN video_quality AS q ON q.path = inv.path
            WHERE LOWER(COALESCE(inv.category,'')) = 'video'
              AND (q.updated_utc IS NULL OR q.updated_utc < inv.indexed_utc)
            ORDER BY inv.indexed_utc DESC
            """
        )
        params: List[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cursor = self.conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def _fs_path(self, path: str) -> str:
        candidate = Path(path)
        if candidate.is_absolute() or self.mount_path is None:
            final_path = candidate
        else:
            final_path = self.mount_path / candidate
        return to_fs_path(str(final_path), mode=self.long_path_mode)

    def _unique(self, values: List[Optional[str]]) -> str:
        seen: List[str] = []
        for value in values:
            if not value:
                continue
            token = str(value).strip().lower()
            if not token or token in seen:
                continue
            seen.append(token)
        return ",".join(seen)

    def run(self, *, limit: Optional[int] = None) -> QualitySummary:
        summary = QualitySummary()
        if not ffprobe_available():
            summary.ffprobe_missing = True
            LOGGER.warning("ffprobe not available; skipping quality pipeline")
            return summary
        start = time.perf_counter()
        candidates = self._inventory_candidates(limit=limit)
        total = len(candidates)
        LOGGER.info("Quality pipeline evaluating %d videos", total)
        self._emit(
            {
                "type": "quality",
                "total": total,
                "processed": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
            }
        )
        batch: List[QualityRow] = []
        batch_limit = 16
        for index, row in enumerate(candidates, start=1):
            if self._should_cancel():
                LOGGER.info("Quality pipeline cancelled after %d items", summary.processed)
                break
            inv_path = row.get("path")
            if not isinstance(inv_path, str) or not inv_path:
                summary.skipped += 1
                continue
            fs_path = self._fs_path(inv_path)
            probe_result = run_ffprobe(fs_path, timeout=self.settings.timeout_s)
            if probe_result.ok and probe_result.data is not None:
                quality_row = self._build_row(inv_path, probe_result)
                summary.updated += 1
            else:
                quality_row = self._error_row(inv_path, probe_result)
                summary.errors += 1
            summary.processed += 1
            batch.append(quality_row)
            if len(batch) >= batch_limit:
                self.store.upsert_rows(batch)
                batch.clear()
            elapsed = time.perf_counter() - start
            remaining = total - index
            eta = (elapsed / index) * remaining if index else None
            payload = {
                "type": "quality",
                "total": total,
                "processed": summary.processed,
                "updated": summary.updated,
                "skipped": summary.skipped,
                "errors": summary.errors,
                "eta_s": eta,
                "path": inv_path,
            }
            if probe_result.ok and probe_result.data is not None:
                payload["score"] = quality_row.score
            else:
                payload["error"] = probe_result.error or probe_result.reason
            self._emit(payload)
            if self.gentle_sleep:
                time.sleep(self.gentle_sleep)
        if batch:
            self.store.upsert_rows(batch)
        summary.elapsed_s = time.perf_counter() - start
        self._emit(
            {
                "type": "quality",
                "total": total,
                "processed": summary.processed,
                "updated": summary.updated,
                "skipped": summary.skipped,
                "errors": summary.errors,
                "done": True,
            }
        )
        return summary

    def _build_row(self, path: str, probe_result: ProbeResult) -> QualityRow:
        assert probe_result.data is not None
        data = probe_result.data
        score_result: QualityResult = score_quality(
            QualityInput(probe=data),
            thresholds=self.settings.thresholds,
            labels=self.settings.labels,
        )
        video = data.video
        width = video.width if video and video.width is not None else None
        height = video.height if video and video.height is not None else None
        video_codec = video.codec if video else None
        video_bitrate = video.bit_rate_kbps if video and video.bit_rate_kbps else None
        audio_codecs = self._unique([stream.codec for stream in data.audio_streams])
        audio_langs = self._unique([stream.language for stream in data.audio_streams])
        subs_langs = self._unique([stream.language for stream in data.subtitle_streams])
        max_channels = None
        for stream in data.audio_streams:
            if stream.channels is None:
                continue
            value = int(stream.channels)
            if max_channels is None or value > max_channels:
                max_channels = value
        reasons_json = reasons_to_json(score_result.reasons)
        return QualityRow(
            path=path,
            container=data.container,
            duration_s=data.duration_s,
            width=width,
            height=height,
            video_codec=video_codec,
            video_bitrate_kbps=video_bitrate,
            audio_codecs=audio_codecs or None,
            audio_channels_max=max_channels,
            audio_langs=audio_langs or None,
            subs_present=1 if data.subtitle_streams else 0,
            subs_langs=subs_langs or None,
            score=score_result.score,
            reasons_json=reasons_json,
            updated_utc=QualityStore.utc_now(),
        )

    def _error_row(self, path: str, probe_result: ProbeResult) -> QualityRow:
        reasons = {
            "status": probe_result.reason or "probe_error",
        }
        if probe_result.error:
            reasons["message"] = probe_result.error
        return QualityRow(
            path=path,
            container=None,
            duration_s=None,
            width=None,
            height=None,
            video_codec=None,
            video_bitrate_kbps=None,
            audio_codecs=None,
            audio_channels_max=None,
            audio_langs=None,
            subs_present=0,
            subs_langs=None,
            score=0,
            reasons_json=reasons_to_json(reasons),
            updated_utc=QualityStore.utc_now(),
        )


def run_for_shard(
    shard_path: Path,
    *,
    settings: QualitySettings,
    progress_callback: Optional[ProgressCallback] = None,
    cancellation: Optional[CancellationToken] = None,
    gentle_sleep: float = 0.0,
    mount_path: Optional[Path] = None,
    long_path_mode: str = "auto",
    limit: Optional[int] = None,
) -> QualitySummary:
    conn = connect(shard_path, read_only=False, check_same_thread=False)
    try:
        runner = QualityRunner(
            conn,
            settings=settings,
            mount_path=mount_path,
            progress_callback=progress_callback,
            cancellation=cancellation,
            gentle_sleep=gentle_sleep,
            long_path_mode=long_path_mode,
        )
        return runner.run(limit=limit)
    finally:
        conn.close()


__all__ = [
    "QualityRunner",
    "QualitySettings",
    "QualitySummary",
    "run_for_shard",
]
