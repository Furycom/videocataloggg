"""Automated visual review generation pipeline."""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, Optional, Sequence, Tuple

from core.db import connect
from core.paths import get_shard_db_path, get_shards_dir, resolve_working_dir
from structure import tv_review

from .contact_sheet import ContactSheetBuilder, ContactSheetConfig
from .frame_sampler import FrameSample, FrameSampler, FrameSamplerConfig
from .store import VisualReviewStore, VisualReviewStoreConfig
from .pillow_support import (
    PillowUnavailableError,
    ensure_pillow,
    load_pillow_image,
)

LOGGER = logging.getLogger("videocatalog.visualreview.run")

ProgressCallback = Callable[["ReviewProgress"], None]
CancelCallback = Callable[[], bool]


@dataclass(slots=True)
class VisualReviewSettings:
    """User configurable options for the visual review pipeline."""

    enable: bool = False
    frames_per_video: int = 9
    max_thumb_px: int = 640
    thumbnail_format: str = "JPEG"
    thumbnail_quality: int = 85
    max_thumbnail_bytes: int = 600_000
    max_contact_sheet_bytes: int = 2_000_000
    thumbnail_retention: int = 800
    sheet_retention: int = 400
    max_db_blob_mb: int = 256
    batch_size: int = 3
    sleep_seconds: float = 1.0
    ffmpeg_path: Optional[Path] = None
    prefer_pyav: bool = False
    scene_threshold: float = 27.0
    min_scene_len: float = 24.0
    fallback_percentages: Tuple[float, ...] = (
        0.05,
        0.20,
        0.35,
        0.50,
        0.65,
        0.80,
        0.95,
    )
    cropdetect: bool = False
    cropdetect_frames: int = 12
    cropdetect_skip_seconds: float = 1.0
    cropdetect_round: int = 16
    allow_hwaccel: bool = True
    hwaccel_policy: str = "AUTO"
    sheet_columns: int = 4
    sheet_max_rows: Optional[int] = None
    sheet_cell_px: Tuple[int, int] = (320, 180)
    sheet_background: Tuple[int, int, int] = (16, 16, 16)
    sheet_margin: int = 24
    sheet_padding: int = 6
    sheet_format: str = "WEBP"
    sheet_quality: int = 80
    sheet_optimize: bool = True

    @classmethod
    def from_mapping(cls, mapping: Optional[Dict[str, object]]) -> "VisualReviewSettings":
        data = dict(mapping or {})

        def _get_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
            value = data.get(name, default)
            try:
                intval = int(value)
            except (TypeError, ValueError):
                return default
            if minimum is not None:
                intval = max(minimum, intval)
            return intval

        def _get_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
            value = data.get(name, default)
            try:
                floatval = float(value)
            except (TypeError, ValueError):
                return default
            if minimum is not None:
                floatval = max(minimum, floatval)
            return floatval

        def _get_bool(name: str, default: bool) -> bool:
            value = data.get(name, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"1", "true", "yes", "on"}:
                    return True
                if lowered in {"0", "false", "no", "off"}:
                    return False
            return bool(value)

        def _get_sequence(name: str, length: int) -> Optional[Tuple[int, ...]]:
            value = data.get(name)
            if not isinstance(value, (list, tuple)) or len(value) != length:
                return None
            items: list[int] = []
            for item in value:
                try:
                    items.append(int(item))
                except (TypeError, ValueError):
                    return None
            return tuple(items)

        def _get_percentages(name: str, default: Tuple[float, ...]) -> Tuple[float, ...]:
            value = data.get(name)
            if not isinstance(value, (list, tuple)):
                return default
            result: list[float] = []
            for item in value:
                try:
                    result.append(float(item))
                except (TypeError, ValueError):
                    continue
            return tuple(result) if result else default

        ffmpeg_value = data.get("ffmpeg_path")
        if isinstance(ffmpeg_value, str) and ffmpeg_value.strip():
            ffmpeg_path = Path(ffmpeg_value).expanduser()
        else:
            ffmpeg_path = None

        cell = _get_sequence("sheet_cell_px", 2) or (320, 180)
        cell = (max(16, cell[0]), max(16, cell[1]))

        background_value = data.get("sheet_background")
        if (
            isinstance(background_value, (list, tuple))
            and len(background_value) == 3
        ):
            parsed: list[int] = []
            for item in background_value:
                try:
                    parsed.append(max(0, min(255, int(item))))
                except (TypeError, ValueError):
                    parsed = []
                    break
            background: Tuple[int, int, int]
            background = tuple(parsed) if len(parsed) == 3 else (16, 16, 16)
        else:
            background = (16, 16, 16)

        max_rows_value = data.get("sheet_max_rows")
        if max_rows_value is None:
            max_rows: Optional[int] = None
        else:
            try:
                max_rows = max(1, int(max_rows_value))
            except (TypeError, ValueError):
                max_rows = None

        fallback = _get_percentages("fallback_percentages", cls().fallback_percentages)

        return cls(
            enable=_get_bool("enable", False),
            frames_per_video=_get_int("frames_per_video", 9, minimum=1),
            max_thumb_px=_get_int("max_thumb_px", 640, minimum=32),
            thumbnail_format=str(data.get("thumbnail_format", "JPEG") or "JPEG"),
            thumbnail_quality=_get_int("thumbnail_quality", 85, minimum=1),
            max_thumbnail_bytes=_get_int("max_thumbnail_bytes", 600000, minimum=1),
            max_contact_sheet_bytes=_get_int(
                "max_contact_sheet_bytes", 2000000, minimum=1
            ),
            thumbnail_retention=_get_int("thumbnail_retention", 800, minimum=0),
            sheet_retention=_get_int("sheet_retention", 400, minimum=0),
            max_db_blob_mb=_get_int("max_db_blob_mb", 256, minimum=0),
            batch_size=_get_int("batch_size", 3, minimum=1),
            sleep_seconds=_get_float("sleep_seconds", 1.0, minimum=0.0),
            ffmpeg_path=ffmpeg_path,
            prefer_pyav=_get_bool("prefer_pyav", False),
            scene_threshold=_get_float("scene_threshold", 27.0, minimum=0.0),
            min_scene_len=_get_float("min_scene_len", 24.0, minimum=0.0),
            fallback_percentages=fallback,
            cropdetect=_get_bool("cropdetect", False),
            cropdetect_frames=_get_int("cropdetect_frames", 12, minimum=1),
            cropdetect_skip_seconds=_get_float(
                "cropdetect_skip_seconds", 1.0, minimum=0.0
            ),
            cropdetect_round=_get_int("cropdetect_round", 16, minimum=1),
            allow_hwaccel=_get_bool("allow_hwaccel", True),
            hwaccel_policy=str(data.get("hwaccel_policy", "AUTO") or "AUTO"),
            sheet_columns=_get_int("sheet_columns", 4, minimum=1),
            sheet_max_rows=max_rows,
            sheet_cell_px=cell,
            sheet_background=background,
            sheet_margin=_get_int("sheet_margin", 24, minimum=0),
            sheet_padding=_get_int("sheet_padding", 6, minimum=0),
            sheet_format=str(data.get("sheet_format", "WEBP") or "WEBP"),
            sheet_quality=_get_int("sheet_quality", 80, minimum=1),
            sheet_optimize=_get_bool("sheet_optimize", True),
        )

    def to_runner_config(
        self,
        *,
        working_dir: Optional[Path] = None,
        mounts: Optional[Dict[str, Path]] = None,
        shard_labels: Optional[Sequence[str]] = None,
    ) -> "ReviewRunnerConfig":
        sampler_config = FrameSamplerConfig(
            ffmpeg_path=self.ffmpeg_path,
            prefer_pyav=self.prefer_pyav,
            max_frames=self.frames_per_video,
            scene_threshold=self.scene_threshold,
            min_scene_len=self.min_scene_len,
            fallback_percentages=self.fallback_percentages,
            cropdetect=self.cropdetect,
            cropdetect_frames=self.cropdetect_frames,
            cropdetect_skip_seconds=self.cropdetect_skip_seconds,
            cropdetect_round=self.cropdetect_round,
            hwaccel_policy=self.hwaccel_policy,
            allow_hwaccel=self.allow_hwaccel,
        )
        contact_config = ContactSheetConfig(
            columns=self.sheet_columns,
            max_rows=self.sheet_max_rows,
            cell_size=self.sheet_cell_px,
            background=self.sheet_background,
            margin=self.sheet_margin,
            padding=self.sheet_padding,
            format=self.sheet_format,
            quality=self.sheet_quality,
            optimize=self.sheet_optimize,
        )
        store_config = VisualReviewStoreConfig(
            max_thumbnail_bytes=self.max_thumbnail_bytes,
            max_contact_sheet_bytes=self.max_contact_sheet_bytes,
            thumbnail_retention=self.thumbnail_retention,
            sheet_retention=self.sheet_retention,
            max_db_blob_mb=self.max_db_blob_mb,
        )
        mounts_payload = dict(mounts or {})
        if shard_labels is not None:
            labels: Optional[Sequence[str]] = list(shard_labels)
        else:
            labels = None
        thumbnail_limit = (self.max_thumb_px, self.max_thumb_px)
        return ReviewRunnerConfig(
            working_dir=working_dir or resolve_working_dir(),
            mounts=mounts_payload,
            shard_labels=labels,
            batch_size=self.batch_size,
            sleep_seconds=self.sleep_seconds,
            thumbnail_format=self.thumbnail_format,
            thumbnail_quality=max(1, min(self.thumbnail_quality, 100)),
            thumbnail_max_size=thumbnail_limit,
            frame_sampler=sampler_config,
            contact_sheet=contact_config,
            store=store_config,
        )


def load_visualreview_settings(data: Dict[str, object] | None) -> VisualReviewSettings:
    """Extract the visual review settings section from the global mapping."""

    section = None
    if isinstance(data, dict):
        candidate = data.get("visualreview")
        if isinstance(candidate, dict):
            section = candidate
    return VisualReviewSettings.from_mapping(section)


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
        if ensure_pillow(LOGGER):
            try:
                pillow_image = load_pillow_image()
            except PillowUnavailableError:
                pillow_image = None
            if pillow_image is not None:
                try:
                    image.thumbnail(self._thumbnail_size, pillow_image.Resampling.LANCZOS)
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


@dataclass(slots=True)
class QueueProcessSummary:
    """Aggregated metrics for a visual review queue run."""

    processed_videos: int = 0
    extracted_frames: int = 0
    bytes_written: int = 0
    errors: int = 0
    skipped: int = 0


def _sum_blob_bytes(conn: sqlite3.Connection, table: str) -> int:
    try:
        cursor = conn.execute(
            f"SELECT COALESCE(SUM(LENGTH(image_blob)), 0) FROM {table}"
        )
        row = cursor.fetchone()
    except sqlite3.DatabaseError:
        return 0
    if not row:
        return 0
    value = row[0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sum_frame_count(conn: sqlite3.Connection) -> int:
    try:
        cursor = conn.execute(
            "SELECT COALESCE(SUM(frame_count), 0) FROM contact_sheets"
        )
        row = cursor.fetchone()
    except sqlite3.DatabaseError:
        return 0
    if not row:
        return 0
    value = row[0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _measure_store_totals(shard_path: Path) -> Tuple[int, int]:
    conn = connect(shard_path, read_only=True, check_same_thread=False)
    try:
        thumbs = _sum_blob_bytes(conn, "video_thumbs")
        sheets = _sum_blob_bytes(conn, "contact_sheets")
        frames = _sum_frame_count(conn)
        return thumbs + sheets, frames
    finally:
        conn.close()


def process_queue(
    shard_path: Path,
    *,
    drive_label: str,
    mount_path: Path,
    settings: VisualReviewSettings,
    working_dir: Optional[Path] = None,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[CancelCallback] = None,
) -> QueueProcessSummary:
    """Process a shard review queue and return aggregated metrics."""

    base = Path(mount_path)
    totals_before = _measure_store_totals(shard_path)
    config = settings.to_runner_config(
        working_dir=working_dir or resolve_working_dir(),
        mounts={drive_label: base},
        shard_labels=[drive_label],
    )
    runner = ReviewRunner(config)
    result = runner.run(progress=progress, cancel=cancel)
    totals_after = _measure_store_totals(shard_path)
    bytes_written = max(0, totals_after[0] - totals_before[0])
    frames_written = max(0, totals_after[1] - totals_before[1])
    return QueueProcessSummary(
        processed_videos=result.processed,
        extracted_frames=frames_written,
        bytes_written=bytes_written,
        errors=result.failed,
        skipped=result.skipped,
    )
