"""Automated visual review generation pipeline."""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, Optional, Sequence

from PIL import Image

from core.db import connect
from core.paths import get_shard_db_path, get_shards_dir, resolve_working_dir
from structure import tv_review

from .contact_sheet import ContactSheetBuilder, ContactSheetConfig
from .frame_sampler import FrameSample, FrameSampler, FrameSamplerConfig
from .store import VisualReviewStore, VisualReviewStoreConfig

LOGGER = logging.getLogger("videocatalog.visualreview.run")

ProgressCallback = Callable[["ReviewProgress"], None]
CancelCallback = Callable[[], bool]


@dataclass(slots=True)
class ReviewItem:
    """Descriptor for a single review queue entry."""

    drive_label: str
    item_type: str
    item_key: str
    video_path: Path
    confidence: Optional[float] = None


@dataclass(slots=True)
class ReviewProgress:
    """Progress payload emitted to observers."""

    processed: int = 0
    skipped: int = 0
    failed: int = 0
    total_attempted: int = 0
    last_item: Optional[ReviewItem] = None


@dataclass(slots=True)
class ReviewBatchResult:
    """Summary of a review batch run."""

    processed: int = 0
    thumbnails_written: int = 0
    sheets_written: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(slots=True)
class ReviewRunnerConfig:
    """Runtime configuration for :class:`ReviewRunner`."""

    working_dir: Path = field(default_factory=resolve_working_dir)
    mounts: Dict[str, Path] = field(default_factory=dict)
    shard_labels: Optional[Sequence[str]] = None
    batch_size: int = 3
    sleep_seconds: float = 1.0
    thumbnail_format: str = "JPEG"
    thumbnail_quality: int = 85
    thumbnail_max_size: Sequence[int] = field(default_factory=lambda: (640, 360))
    frame_sampler: FrameSamplerConfig = field(default_factory=FrameSamplerConfig)
    contact_sheet: ContactSheetConfig = field(default_factory=ContactSheetConfig)
    store: VisualReviewStoreConfig = field(default_factory=VisualReviewStoreConfig)


class ReviewRunner:
    """High level coordinator that processes review queues in batches."""

    def __init__(
        self,
        config: Optional[ReviewRunnerConfig] = None,
        *,
        frame_sampler: Optional[FrameSampler] = None,
        contact_sheet: Optional[ContactSheetBuilder] = None,
    ) -> None:
        self._config = config or ReviewRunnerConfig()
        sampler_config = self._config.frame_sampler
        if sampler_config.max_frames <= 0:
            sampler_config.max_frames = 6
        sheet_config = self._config.contact_sheet
        if sheet_config.columns <= 0:
            sheet_config.columns = 4
        self._sampler = frame_sampler or FrameSampler(sampler_config)
        self._sheet_builder = contact_sheet or ContactSheetBuilder(sheet_config)
        self._store_config = self._config.store
        self._thumbnail_format = (self._config.thumbnail_format or "JPEG").upper()
        self._thumbnail_quality = max(1, min(self._config.thumbnail_quality, 100))
        limit = list(self._config.thumbnail_max_size)
        if len(limit) != 2:
            limit = [640, 360]
        self._thumbnail_size = (max(32, int(limit[0])), max(32, int(limit[1])))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[CancelCallback] = None,
    ) -> ReviewBatchResult:
        summary = ReviewBatchResult()
        progress_state = ReviewProgress()
        for label, shard_path in self._iter_shards():
            mount = self._config.mounts.get(label)
            if mount is None:
                LOGGER.debug("No mount path configured for drive %s; skipping", label)
                continue
            mount_path = Path(mount)
            if cancel and cancel():
                LOGGER.info("Review run cancelled before processing shard %s", label)
                break
            items = list(
                self._collect_items(label, shard_path, mount_path, self._config.batch_size)
            )
            if not items:
                continue
            with VisualReviewStore(shard_path, config=self._store_config) as store:
                for item in items:
                    if cancel and cancel():
                        LOGGER.info("Review run cancelled during processing of %s", item.item_key)
                        return summary
                    progress_state.total_attempted += 1
                    progress_state.last_item = item
                    try:
                        wrote_sheet, wrote_thumb = self._process_item(store, item)
                    except Exception as exc:  # pragma: no cover - defensive logging
                        LOGGER.exception("Failed to process %s/%s", item.drive_label, item.item_key)
                        summary.failed += 1
                        progress_state.failed += 1
                        if progress:
                            progress(progress_state)
                        continue
                    if wrote_sheet or wrote_thumb:
                        summary.processed += 1
                        progress_state.processed += 1
                        if wrote_sheet:
                            summary.sheets_written += 1
                        if wrote_thumb:
                            summary.thumbnails_written += 1
                    else:
                        summary.skipped += 1
                        progress_state.skipped += 1
                    if progress:
                        progress(progress_state)
                store.cleanup()
            if self._config.sleep_seconds > 0:
                time.sleep(self._config.sleep_seconds)
        return summary

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _iter_shards(self) -> Iterator[tuple[str, Path]]:
        shards_dir = get_shards_dir(self._config.working_dir)
        shards_dir.mkdir(parents=True, exist_ok=True)
        labels: Iterable[str]
        if self._config.shard_labels:
            labels = self._config.shard_labels
        else:
            labels = [path.stem for path in sorted(shards_dir.glob("*.db"))]
        for label in labels:
            shard_path = get_shard_db_path(self._config.working_dir, label)
            if shard_path.exists():
                yield label, shard_path

    def _collect_items(
        self,
        label: str,
        shard_path: Path,
        mount_path: Path,
        batch_size: int,
    ) -> Iterator[ReviewItem]:
        produced = 0
        for item in self._folder_items(label, shard_path, mount_path, batch_size):
            yield item
            produced += 1
            if produced >= batch_size:
                return
        remaining = batch_size - produced
        if remaining <= 0:
            return
        for item in self._tv_items(label, shard_path, mount_path, remaining):
            yield item

    def _folder_items(
        self,
        label: str,
        shard_path: Path,
        mount_path: Path,
        batch_size: int,
    ) -> Iterator[ReviewItem]:
        with connect(shard_path, read_only=True, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(
                    """
                    SELECT folder_path, confidence
                    FROM review_queue
                    ORDER BY confidence ASC, created_utc ASC
                    LIMIT ?
                    """,
                    (batch_size,),
                )
            except sqlite3.DatabaseError:
                return
            rows = cursor.fetchall()
            for row in rows:
                folder_key = row["folder_path"]
                try:
                    video_row = conn.execute(
                        "SELECT main_video_path FROM folder_profile WHERE folder_path = ?",
                        (folder_key,),
                    ).fetchone()
                except sqlite3.DatabaseError:
                    continue
                if not video_row:
                    continue
                main_video = video_row["main_video_path"]
                if not main_video:
                    continue
                abs_video = _resolve_media_path(mount_path, main_video)
                if not abs_video.exists():
                    LOGGER.debug("Video missing for folder %s on %s", folder_key, label)
                    continue
                yield ReviewItem(
                    drive_label=label,
                    item_type="folder",
                    item_key=str(folder_key),
                    video_path=abs_video,
                    confidence=row["confidence"],
                )

    def _tv_items(
        self,
        label: str,
        shard_path: Path,
        mount_path: Path,
        batch_size: int,
    ) -> Iterator[ReviewItem]:
        with connect(shard_path, read_only=True, check_same_thread=False) as conn:
            try:
                rows = tv_review.list_review_queue(conn, limit=batch_size)
            except sqlite3.DatabaseError:
                return
            for row in rows:
                item_type = row["item_type"]
                item_key = row["item_key"]
                if item_type != tv_review.EPISODE:
                    continue
                try:
                    episode_row = conn.execute(
                        "SELECT episode_path FROM tv_episode_profile WHERE episode_path = ?",
                        (item_key,),
                    ).fetchone()
                except sqlite3.DatabaseError:
                    continue
                if not episode_row:
                    continue
                episode_path = episode_row["episode_path"] or item_key
                abs_path = _resolve_media_path(mount_path, episode_path)
                if not abs_path.exists():
                    LOGGER.debug("Episode video missing for %s on %s", episode_path, label)
                    continue
                yield ReviewItem(
                    drive_label=label,
                    item_type="episode",
                    item_key=item_key,
                    video_path=abs_path,
                    confidence=row["confidence"],
                )

    def _process_item(
        self, store: VisualReviewStore, item: ReviewItem
    ) -> tuple[bool, bool]:
        samples = self._sampler.sample(item.video_path)
        if not samples:
            return False, False
        sheet_result = self._sheet_builder.build(samples)
        if not sheet_result:
            return False, False
        contact_format = (sheet_result.format or self._sheet_builder.output_format).upper()
        contact_quality = max(1, int(self._config.contact_sheet.quality))
        sheet_bytes = sheet_result.to_bytes(quality=contact_quality, format=contact_format)
        wrote_sheet = store.upsert_contact_sheet(
            item_type=item.item_type,
            item_key=item.item_key,
            data=sheet_bytes,
            format=contact_format,
            width=sheet_result.width,
            height=sheet_result.height,
            frame_count=sheet_result.frame_count,
        )
        wrote_thumb = self._store_thumbnail(store, item, samples[0])
        return wrote_sheet, wrote_thumb

    def _store_thumbnail(
        self, store: VisualReviewStore, item: ReviewItem, sample: FrameSample
    ) -> bool:
        image = sample.image.copy()
        try:
            image.thumbnail(self._thumbnail_size, Image.Resampling.LANCZOS)
        except Exception:
            pass
        return store.upsert_thumbnail(
            item_type=item.item_type,
            item_key=item.item_key,
            image=image,
            format=self._thumbnail_format,
            quality=self._thumbnail_quality,
        )


def _resolve_media_path(base: Path, candidate: str) -> Path:
    path = Path(candidate)
    if path.is_absolute():
        return path
    return base / path
