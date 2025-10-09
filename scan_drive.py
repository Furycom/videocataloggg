import argparse
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import queue
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

import numpy as np

from analyzers import (
    CaptionConfig,
    CaptionRecord,
    CaptionService,
    CaptionWriter,
    FeatureRecord,
    FeatureWriter,
    ImageEmbedder,
    ImageEmbedderConfig,
    LightAnalysisModelError,
    SemanticAnalyzer,
    SemanticAnalyzerConfig,
    SemanticModelError,
    TranscriptRecord,
    TranscriptWriter,
    TranscriptionConfig,
    TranscriptionService,
    VideoThumbnailAnalyzer,
    ensure_caption_tables,
    ensure_features_table,
    ensure_transcript_tables,
)
from gpu.capabilities import probe_gpu
from gpu.runtime import (
    get_hwaccel_args,
    providers_for_session,
    report_provider_failure,
    select_provider,
)
from core.paths import (
    ensure_working_dir_structure,
    get_catalog_db_path,
    get_shard_db_path,
    get_shards_dir,
    resolve_working_dir,
    safe_label,
)
from core.ann import ANNIndexManager
from core.db import connect
from core.settings import load_settings
from exports import ExportFilters, export_catalog, parse_since
from inventory import InventoryRow, InventoryWriter, categorize, detect_mime
from semantic import (
    SemanticConfig,
    SemanticIndexer,
    SemanticPhaseError,
    SemanticSearcher,
    SemanticTranscriber,
)
from tools import bootstrap_local_bin, probe_tool
from fingerprints import (
    audio_chroma as fp_audio,
    store as fp_store,
    tools as fp_tools,
    video_tmk as fp_video,
    video_vhash as fp_vhash,
)

from perf import (
    RateController,
    enumerate_sleep_range,
    resolve_performance_config,
)
from robust import (
    CancellationToken,
    PathTooLongError,
    clamp_batch_seconds,
    from_fs_path,
    is_hidden,
    is_transient,
    key_for_path,
    normalize_path,
    merge_settings,
    should_ignore,
    to_fs_path,
)

from db_maint import (
    MaintenanceOptions,
    check_integrity,
    light_backup,
    quick_optimize,
    reindex_and_analyze,
    resolve_options,
    update_maintenance_metadata,
    vacuum_if_needed,
)
from structure import StructureProfiler, load_structure_settings as load_structure_config

WORKING_DIR_PATH = resolve_working_dir()
ensure_working_dir_structure(WORKING_DIR_PATH)
bootstrap_local_bin(WORKING_DIR_PATH)
DEFAULT_DB_PATH = str(get_catalog_db_path(WORKING_DIR_PATH))
LOGGER = logging.getLogger("videocatalog.scan")

ALL_TOOLS = ("mediainfo", "ffmpeg", "smartctl")
REQUIRED_TOOLS = ("mediainfo", "ffmpeg")
TOOL_STATUS: Dict[str, dict] = {}
TOOL_PATHS: Dict[str, Optional[str]] = {}


def _refresh_tool_status() -> Dict[str, dict]:
    global TOOL_STATUS, TOOL_PATHS
    statuses: Dict[str, dict] = {}
    for tool in ALL_TOOLS:
        statuses[tool] = probe_tool(tool, WORKING_DIR_PATH)
    TOOL_STATUS = statuses
    TOOL_PATHS = {
        tool: (info.get("path") if info.get("present") else None)
        for tool, info in statuses.items()
    }
    return statuses


def _expand_user_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded)

_tqdm_spec = importlib.util.find_spec("tqdm")
if _tqdm_spec is None:
    def tqdm(iterable, **kwargs):
        return iterable
else:
    tqdm = importlib.import_module("tqdm").tqdm

_blake3_spec = importlib.util.find_spec("blake3")
_blake3_hash = importlib.import_module("blake3").blake3 if _blake3_spec is not None else None

VIDEO_EXTS = {'.mp4','.mkv','.avi','.mov','.wmv','.m4v','.ts','.m2ts','.webm','.mpg','.mpeg'}
AUDIO_EXTS = {'.mp3','.flac','.aac','.m4a','.wav','.wma','.ogg','.opus','.alac','.aiff','.ape','.dsf','.dff'}
AV_EXTS = VIDEO_EXTS | AUDIO_EXTS
IMAGE_EXTS = {
    '.jpg',
    '.jpeg',
    '.png',
    '.gif',
    '.bmp',
    '.tif',
    '.tiff',
    '.webp',
    '.heic',
}


@dataclass(slots=True)
class FileInfo:
    path: str  # original display/DB path
    fs_path: str  # path used for filesystem operations (may include long-path prefix)
    size_bytes: int
    mtime_utc: str
    is_av: bool
    existing_id: Optional[int] = None
    was_deleted: bool = False


@dataclass(slots=True)
class WorkerResult:
    info: FileInfo
    hash_value: Optional[str]
    media_blob: Optional[str]
    integrity_ok: Optional[int]
    error_message: Optional[str] = None
    media_metadata: Optional[dict] = None


@dataclass
class LightAnalysisSettings:
    enabled: bool
    model_path: Path
    max_video_frames: int
    prefer_ffmpeg: bool
    semantic_enabled: bool
    semantic_model: str
    semantic_pretrained: str
    scene_threshold: float
    scene_min_len: float
    transcription_enabled: bool
    transcription_model: str
    transcription_max_duration: Optional[float]
    transcription_beam_size: int
    transcription_vad: bool
    caption_enabled: bool
    caption_model: str
    caption_max_length: int
    ann_enabled: bool
    ann_backend: str


@dataclass
class GPUSettings:
    policy: str
    allow_hwaccel_video: bool
    min_free_vram_mb: int
    max_gpu_workers: int


@dataclass
class FingerprintSettings:
    enable_video_tmk: bool
    enable_audio_chroma: bool
    enable_video_vhash_prefilter: bool
    phase_mode: str
    max_concurrency: int
    io_gentle_ms: int
    batch_size: int
    tmk_similarity_threshold: float
    chroma_match_threshold: float
    tmk_bin_path: Optional[Path]
    fpcalc_path: Optional[Path]


class LightAnalysisPipeline:
    def __init__(
        self,
        *,
        settings: LightAnalysisSettings,
        gpu_settings: GPUSettings,
        connection: sqlite3.Connection,
        ffmpeg_path: Optional[str],
        perf_profile: str,
        progress_callback: Optional[Callable[[dict], None]],
        start_time: float,
        drive_label: str,
    ) -> None:
        self._requested = bool(settings.enabled)
        self._settings = settings
        self._gpu = gpu_settings
        self._conn = connection
        self._ffmpeg_path = ffmpeg_path
        self._profile = perf_profile
        self._progress_callback = progress_callback
        self._start = start_time
        self._drive_label = drive_label
        self._last_emit = 0.0
        self._writer: Optional[FeatureWriter] = None
        self._embedder: Optional[ImageEmbedder] = None
        self._video: Optional[VideoThumbnailAnalyzer] = None
        self._semantic: Optional[SemanticAnalyzer] = None
        self._transcriber: Optional[TranscriptionService] = None
        self._captioner: Optional[CaptionService] = None
        self._transcript_writer: Optional[TranscriptWriter] = None
        self._caption_writer: Optional[CaptionWriter] = None
        self._ann_manager: Optional[ANNIndexManager] = None
        self._active = False
        self._error: Optional[str] = None
        self._warning: Optional[str] = None
        self._gpu_provider: str = "CPU"
        self._gpu_hwaccel = False
        self._ffmpeg_hwaccel_args: list[str] = []
        self._features_updated = False
        self._transcripts_written = 0
        self._captions_written = 0

    def prepare(self) -> None:
        if not self._requested:
            return
        ensure_features_table(self._conn)
        caps = probe_gpu()
        provider = select_provider(
            self._gpu.policy,
            min_free_vram_mb=self._gpu.min_free_vram_mb,
            caps=caps,
        )
        hwaccel_args = get_hwaccel_args(
            self._gpu.policy,
            allow_hwaccel=self._gpu.allow_hwaccel_video,
            caps=caps,
        )
        providers = providers_for_session(provider)
        self._gpu_provider = provider
        self._ffmpeg_hwaccel_args = list(hwaccel_args)
        self._gpu_hwaccel = bool(self._ffmpeg_hwaccel_args)

        semantic_ready = False
        if self._settings.semantic_enabled:
            try:
                self._semantic = SemanticAnalyzer(
                    SemanticAnalyzerConfig(
                        model_name=self._settings.semantic_model,
                        pretrained=self._settings.semantic_pretrained,
                        device_policy=self._gpu.policy,
                        ffmpeg_path=Path(self._ffmpeg_path) if self._ffmpeg_path else None,
                        max_video_frames=self._settings.max_video_frames,
                        scene_threshold=self._settings.scene_threshold,
                        min_scene_len=self._settings.scene_min_len,
                    )
                )
                semantic_ready = True
            except SemanticModelError as exc:
                self._warning = (self._warning or str(exc))
            except Exception as exc:  # pragma: no cover - defensive guard
                self._warning = (self._warning or f"Semantic analysis unavailable: {exc}")

        if not semantic_ready:
            try:
                self._embedder = ImageEmbedder(
                    ImageEmbedderConfig(
                        model_path=self._settings.model_path,
                        providers=providers,
                        primary_provider=providers[0] if providers else "CPUExecutionProvider",
                    )
                )
            except LightAnalysisModelError as exc:
                if provider != "CPU":
                    report_provider_failure(provider, exc)
                    fallback_providers = providers_for_session("CPU")
                    self._gpu_provider = "CPU"
                    self._ffmpeg_hwaccel_args = []
                    self._gpu_hwaccel = False
                    if not self._warning:
                        self._warning = "GPU acceleration unavailable — using CPU."
                    try:
                        self._embedder = ImageEmbedder(
                            ImageEmbedderConfig(
                                model_path=self._settings.model_path,
                                providers=fallback_providers,
                                primary_provider="CPUExecutionProvider",
                            )
                        )
                    except LightAnalysisModelError as cpu_exc:
                        self._error = str(cpu_exc)
                        self._emit_status("error", self._error)
                        self._log_gpu_status()
                        return
                else:
                    self._error = str(exc)
                    self._emit_status("error", self._error)
                    self._log_gpu_status()
                    return
            except Exception as exc:  # pragma: no cover - defensive guard
                self._error = f"Light analysis unavailable: {exc}"
                self._emit_status("error", self._error)
                self._log_gpu_status()
                return

        self._writer = FeatureWriter(self._conn, batch_size=48)

        if self._semantic is None and self._ffmpeg_path:
            self._video = VideoThumbnailAnalyzer(
                embedder=self._embedder,
                ffmpeg_path=self._ffmpeg_path,
                prefer_ffmpeg=self._settings.prefer_ffmpeg,
                max_frames=self._settings.max_video_frames,
                rate_limit_profile=self._profile,
                hwaccel_args=self._ffmpeg_hwaccel_args,
            )
        elif self._semantic is None and not self._ffmpeg_path:
            self._warning = "FFmpeg not available — video thumbnails skipped."
            self._emit_status("warning", self._warning)
            self._ffmpeg_hwaccel_args = []
            self._gpu_hwaccel = False

        if self._settings.transcription_enabled:
            try:
                ensure_transcript_tables(self._conn)
            except sqlite3.Error as exc:
                LOGGER.warning("Unable to prepare transcripts table: %s", exc)
            transcriber = TranscriptionService(
                TranscriptionConfig(
                    enabled=True,
                    model_size=self._settings.transcription_model,
                    device_policy=self._gpu.policy,
                    max_duration=self._settings.transcription_max_duration,
                    beam_size=self._settings.transcription_beam_size,
                    vad_filter=self._settings.transcription_vad,
                )
            )
            if transcriber.available:
                self._transcriber = transcriber
                self._transcript_writer = TranscriptWriter(self._conn, batch_size=24)
            elif transcriber.last_error:
                self._warning = self._warning or f"Transcription unavailable: {transcriber.last_error}"

        if self._settings.caption_enabled:
            try:
                ensure_caption_tables(self._conn)
            except sqlite3.Error as exc:
                LOGGER.warning("Unable to prepare captions table: %s", exc)
            captioner = CaptionService(
                CaptionConfig(
                    enabled=True,
                    model_name=self._settings.caption_model,
                    device_policy=self._gpu.policy,
                    max_length=self._settings.caption_max_length,
                )
            )
            if captioner.available:
                self._captioner = captioner
                self._caption_writer = CaptionWriter(self._conn, batch_size=24)
            elif self._settings.caption_enabled:
                self._warning = self._warning or "Captioning unavailable"

        if self._settings.ann_enabled:
            label = safe_label(self._drive_label or "default")
            self._ann_manager = ANNIndexManager(WORKING_DIR_PATH, label, backend=self._settings.ann_backend)

        self._log_gpu_status()
        self._active = True

    def _log_gpu_status(self) -> None:
        label_map = {
            "CUDAExecutionProvider": "CUDA",
            "DmlExecutionProvider": "DML",
            "CPU": "CPU",
        }
        label = label_map.get(self._gpu_provider, self._gpu_provider)
        LOGGER.info(
            "GPU: policy=%s provider=%s hwaccel=%s",
            self._gpu.policy,
            label,
            "on" if self._gpu_hwaccel else "off",
        )

    @staticmethod
    def _normalize_vector(vector: np.ndarray) -> np.ndarray:
        arr = np.asarray(vector, dtype=np.float32)
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr = arr / norm
        return arr.astype(np.float32, copy=False)

    def process(self, info: FileInfo, metadata: Optional[dict] = None) -> None:
        if not self._active or not self._writer:
            return
        try:
            suffix = Path(info.path).suffix.lower()
        except Exception:
            return
        if suffix in IMAGE_EXTS:
            vector: Optional[np.ndarray] = None
            if self._semantic is not None:
                vector = self._semantic.encode_image_path(Path(info.fs_path))
            elif self._embedder is not None:
                vector = self._embedder.embed_path(Path(info.fs_path))
            if vector is not None:
                normalized = self._normalize_vector(vector)
                record = FeatureRecord(
                    path=info.path,
                    kind="image",
                    vector=normalized,
                    frames_used=1,
                )
                self._writer.add(record)
                self._features_updated = True
                if self._captioner and self._caption_writer:
                    caption = self._captioner.generate(Path(info.fs_path))
                    if caption:
                        self._caption_writer.add(
                            CaptionRecord(
                                path=info.path,
                                content=caption,
                                model=self._settings.caption_model,
                            )
                        )
                        self._captions_written += 1
                self._emit_progress()
        elif suffix in VIDEO_EXTS:
            vector: Optional[np.ndarray] = None
            frames_used = 0
            if self._semantic is not None:
                vector, frames_used = self._semantic.encode_video(Path(info.fs_path))
            elif self._video is not None:
                vector, frames_used = self._video.extract_features(Path(info.fs_path))
            if vector is None:
                return
            normalized = self._normalize_vector(vector)
            record = FeatureRecord(
                path=info.path,
                kind="video",
                vector=normalized,
                frames_used=max(1, frames_used),
            )
            self._writer.add(record)
            self._features_updated = True
            self._emit_progress()

        if (
            self._transcriber
            and self._transcript_writer
            and suffix in AV_EXTS
        ):
            duration = _extract_duration(metadata)
            result = self._transcriber.transcribe(Path(info.fs_path), duration=duration)
            if result:
                text, language = result
                self._transcript_writer.add(
                    TranscriptRecord(path=info.path, content=text, language=language)
                )
                self._transcripts_written += 1

    def finalize(self) -> Dict[str, object]:
        summary = {
            "requested": self._requested,
            "active": self._active,
            "images": 0,
            "videos": 0,
            "avg_dim": 0,
            "provider": self._gpu_provider,
            "gpu_policy": self._gpu.policy,
            "hwaccel": self._gpu_hwaccel,
        }
        if not self._active or not self._writer:
            if self._error:
                summary["status"] = "error"
                summary["message"] = self._error
            elif self._warning:
                summary["status"] = "skipped"
                summary["message"] = self._warning
            else:
                summary["status"] = "skipped"
                if self._requested and not self._error:
                    summary["message"] = "Light analysis disabled."
            return summary
        self._writer.close()
        summary["images"] = self._writer.total_images
        summary["videos"] = self._writer.total_videos
        summary["avg_dim"] = self._writer.average_dimension()
        summary["status"] = "ok"
        if self._warning:
            summary["warning"] = self._warning
        if self._transcript_writer:
            self._transcript_writer.close()
            summary["transcripts"] = self._transcript_writer.total_written
        if self._caption_writer:
            self._caption_writer.close()
            summary["captions"] = self._caption_writer.total_written
        if self._ann_manager and self._settings.ann_enabled:
            try:
                summary["ann"] = self._ann_manager.rebuild_from_db(self._conn)
            except Exception as exc:
                summary["ann"] = {"error": str(exc)}
        self._emit_progress(force=True)
        return summary

    def _emit_progress(self, force: bool = False) -> None:
        if not self._writer:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < 5:
            return
        elapsed = int(time.perf_counter() - self._start)
        payload = {
            "type": "progress",
            "phase": "light-analysis",
            "elapsed_s": elapsed,
            "light_images": self._writer.total_images,
            "light_videos": self._writer.total_videos,
            "light_avg_dim": self._writer.average_dimension(),
        }
        if self._transcripts_written:
            payload["light_transcripts"] = self._transcripts_written
        if self._captions_written:
            payload["light_captions"] = self._captions_written
        if self._progress_callback is not None:
            try:
                self._progress_callback(payload)
            except Exception:
                pass
        else:
            try:
                print(json.dumps(payload), flush=True)
            except Exception:
                pass
        self._last_emit = now

    def _emit_status(self, status: str, message: str) -> None:
        payload = {"type": "light_analysis_status", "status": status, "message": message}
        if self._progress_callback is not None:
            try:
                self._progress_callback(payload)
            except Exception:
                pass
        else:
            try:
                print(json.dumps(payload), flush=True)
            except Exception:
                pass


@dataclass(slots=True)
class _FingerprintTask:
    info: FileInfo
    metadata: Optional[dict]


class FingerprintPipeline:
    def __init__(
        self,
        *,
        settings: FingerprintSettings,
        shard_path: Path,
        progress_callback: Optional[Callable[[dict], None]],
        start_time: float,
        perf_profile: str,
        cancel_token: CancellationToken,
    ) -> None:
        self._settings = settings
        self._shard_path = shard_path
        self._progress_callback = progress_callback
        self._start = start_time
        self._last_emit = 0.0
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._sentinel = object()
        self._threads: List[threading.Thread] = []
        self._pending: List[_FingerprintTask] = []
        self._prepared = False
        self._enabled = False
        self._video_enabled = False
        self._audio_enabled = False
        self._prefilter_enabled = False
        self._tmk_toolchain: Optional[fp_tools.TMKToolchain] = None
        self._chromaprint_tool: Optional[fp_tools.ChromaprintTool] = None
        self._stats = {"video": 0, "audio": 0, "vhash": 0}
        self._lock = threading.Lock()
        self._errors: List[str] = []
        self._cancel = cancel_token
        profile = str(perf_profile or "").upper()
        base_sleep = max(0, int(settings.io_gentle_ms)) / 1000.0
        if profile == "NETWORK":
            base_sleep *= 2.0
        elif profile == "USB":
            base_sleep *= 1.5
        self._sleep_seconds = base_sleep
        self._batch_size = max(1, int(settings.batch_size))

    def prepare(self, connection: sqlite3.Connection) -> None:
        if self._prepared:
            return
        self._prepared = True
        fp_store.ensure_schema(connection)

        if self._settings.phase_mode == "off":
            self._enabled = False
            return

        if self._settings.enable_video_tmk:
            override = str(self._settings.tmk_bin_path) if self._settings.tmk_bin_path else None
            self._tmk_toolchain = fp_tools.resolve_tmk_toolchain(override)
            if not self._tmk_toolchain:
                message = "Tool missing: TMK+PDQF (install facebookresearch TMK binaries)"
                self._errors.append(message)
                LOGGER.error(message)
            else:
                self._video_enabled = True
                if self._tmk_toolchain.version:
                    LOGGER.info("TMK+PDQF ready (%s)", self._tmk_toolchain.version)
        if self._settings.enable_audio_chroma:
            override = str(self._settings.fpcalc_path) if self._settings.fpcalc_path else None
            self._chromaprint_tool = fp_tools.resolve_chromaprint(override)
            if not self._chromaprint_tool:
                message = "Tool missing: Chromaprint fpcalc (install chromaprint or set path)"
                self._errors.append(message)
                LOGGER.error(message)
            else:
                self._audio_enabled = True
                if self._chromaprint_tool.version:
                    LOGGER.info("Chromaprint ready (%s)", self._chromaprint_tool.version)

        self._prefilter_enabled = bool(
            self._settings.enable_video_vhash_prefilter and fp_tools.have_videohash()
        )
        if self._settings.enable_video_vhash_prefilter and not self._prefilter_enabled:
            self._errors.append("videohash library not installed; prefilter disabled.")

        self._enabled = any([self._video_enabled, self._audio_enabled, self._prefilter_enabled])
        if not self._enabled:
            return
        LOGGER.info(
            "Fingerprint pipeline active: workers=%d, batch=%d, sleep=%.0fms, phase=%s",
            self._settings.max_concurrency,
            self._batch_size,
            self._sleep_seconds * 1000.0,
            self._settings.phase_mode,
        )

    def submit(self, result: WorkerResult) -> None:
        if not self._enabled:
            return
        info = result.info
        try:
            suffix = Path(info.path).suffix.lower()
        except Exception:
            return
        wants_video = self._video_enabled and suffix in VIDEO_EXTS
        wants_audio = self._audio_enabled and suffix in AUDIO_EXTS
        wants_prefilter = self._prefilter_enabled and suffix in VIDEO_EXTS
        if not any([wants_video, wants_audio, wants_prefilter]):
            return
        task = _FingerprintTask(info=info, metadata=result.media_metadata)
        if self._settings.phase_mode == "during-scan":
            self._ensure_workers()
            self._queue.put(task)
        else:
            self._pending.append(task)

    def flush(self) -> None:
        if not self._enabled:
            return
        if self._pending:
            self._ensure_workers()
            for task in self._pending:
                self._queue.put(task)
            self._pending.clear()
        if not self._threads:
            return
        self._queue.join()

    def finalize(self) -> Dict[str, object]:
        summary: Dict[str, object] = {
            "status": "skipped" if not self._enabled else "ok",
            "video": self._stats["video"],
            "audio": self._stats["audio"],
            "vhash": self._stats["vhash"],
        }
        if self._errors:
            summary["errors"] = list(self._errors)
        if not self._enabled:
            summary["status"] = "skipped"
            return summary
        for _ in self._threads:
            self._queue.put(self._sentinel)
        for thread in self._threads:
            thread.join()
        summary["status"] = "ok"
        self._emit_progress(force=True)
        return summary

    def _ensure_workers(self) -> None:
        if self._threads:
            return
        for idx in range(self._settings.max_concurrency):
            thread = threading.Thread(target=self._worker, name=f"fp-worker-{idx+1}")
            thread.daemon = True
            thread.start()
            self._threads.append(thread)

    def _worker(self) -> None:
        store = fp_store.FingerprintStore(self._shard_path)
        fingerprinter = fp_video.TMKVideoFingerprinter(self._tmk_toolchain)
        chroma = fp_audio.ChromaprintGenerator(self._chromaprint_tool)
        pending: List[_FingerprintTask] = []
        try:
            while True:
                if self._cancel.is_set() and not pending:
                    break
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if pending:
                        self._process_batch(pending, store, fingerprinter, chroma)
                        for _ in pending:
                            self._queue.task_done()
                        pending.clear()
                        if self._sleep_seconds:
                            time.sleep(self._sleep_seconds)
                    if self._cancel.is_set():
                        break
                    continue
                if item is self._sentinel:
                    self._queue.task_done()
                    if pending:
                        self._process_batch(pending, store, fingerprinter, chroma)
                        for _ in pending:
                            self._queue.task_done()
                        pending.clear()
                    break
                if not isinstance(item, _FingerprintTask):
                    self._queue.task_done()
                    continue
                pending.append(item)
                if len(pending) >= self._batch_size:
                    self._process_batch(pending, store, fingerprinter, chroma)
                    for _ in pending:
                        self._queue.task_done()
                    pending.clear()
                    if self._sleep_seconds:
                        time.sleep(self._sleep_seconds)
                if self._cancel.is_set() and not pending:
                    break
        finally:
            if pending:
                self._process_batch(pending, store, fingerprinter, chroma)
                for _ in pending:
                    self._queue.task_done()
                pending.clear()
            store.close()

    def _process_batch(
        self,
        tasks: List[_FingerprintTask],
        store: fp_store.FingerprintStore,
        fingerprinter: fp_video.TMKVideoFingerprinter,
        chroma: fp_audio.ChromaprintGenerator,
    ) -> None:
        if not tasks:
            return
        try:
            with store.batch():
                for task in tasks:
                    if self._cancel.is_set():
                        break
                    self._process_task(task, store, fingerprinter, chroma)
        except Exception as exc:
            with self._lock:
                self._errors.append(str(exc))

    def _process_task(
        self,
        task: _FingerprintTask,
        store: fp_store.FingerprintStore,
        fingerprinter: fp_video.TMKVideoFingerprinter,
        chroma: fp_audio.ChromaprintGenerator,
    ) -> None:
        info = task.info
        duration_hint = _extract_duration(task.metadata)
        suffix = Path(info.path).suffix.lower()
        errors: List[str] = []
        try:
            if self._prefilter_enabled and suffix in VIDEO_EXTS and not self._cancel.is_set():
                if not store.has_vhash(info.path):
                    vhash = fp_vhash.compute_vhash(Path(info.fs_path))
                    if vhash:
                        store.upsert_video_vhash(path=info.path, vhash=vhash.hash64)
                        with self._lock:
                            self._stats["vhash"] += 1
            if self._video_enabled and suffix in VIDEO_EXTS and not self._cancel.is_set():
                if not store.has_video_signature(info.path):
                    signature = fingerprinter.compute(
                        Path(info.fs_path), duration_hint=duration_hint
                    )
                    if signature:
                        try:
                            store.upsert_video_signature_from_file(
                                path=info.path,
                                duration=signature.duration_seconds,
                                source_path=signature.signature_path,
                                version=signature.tool_version,
                            )
                        finally:
                            signature.cleanup()
                        with self._lock:
                            self._stats["video"] += 1
            if self._audio_enabled and suffix in AUDIO_EXTS and not self._cancel.is_set():
                if not store.has_audio_signature(info.path):
                    fingerprint = chroma.compute(Path(info.fs_path))
                    if fingerprint:
                        store.upsert_audio_signature(
                            path=info.path,
                            duration=fingerprint.duration_seconds,
                            fingerprint=fingerprint.fingerprint,
                            version=fingerprint.tool_version,
                        )
                        with self._lock:
                            self._stats["audio"] += 1
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if errors:
                with self._lock:
                    self._errors.extend(errors)
            self._emit_progress()

    def _emit_progress(self, *, force: bool = False) -> None:
        if not self._progress_callback:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < 5:
            return
        elapsed = int(time.perf_counter() - self._start)
        payload = {
            "type": "progress",
            "phase": "fingerprinting",
            "elapsed_s": elapsed,
            "fingerprint_video": self._stats["video"],
            "fingerprint_audio": self._stats["audio"],
            "fingerprint_vhash": self._stats["vhash"],
        }
        try:
            self._progress_callback(payload)
        except Exception:
            pass
        self._last_emit = now


def _extract_duration(metadata: Optional[dict]) -> Optional[float]:
    if not isinstance(metadata, dict):
        return None
    duration_fields = []
    if "duration" in metadata:
        duration_fields.append(metadata.get("duration"))
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else None
    if media and isinstance(media.get("track"), list):
        for track in media["track"]:
            if not isinstance(track, dict):
                continue
            if track.get("@type") in {"General", "Video", "Audio"}:
                if "Duration" in track:
                    duration_fields.append(track.get("Duration"))
                if "Duration/String" in track:
                    duration_fields.append(track.get("Duration/String"))
    for value in duration_fields:
        if value is None:
            continue
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                continue
    return None

def _resolve_light_analysis(
    settings: Optional[Dict[str, object]], override: Optional[bool]
) -> LightAnalysisSettings:
    config: Dict[str, object] = {}
    if isinstance(settings, dict):
        maybe = settings.get("light_analysis")
        if isinstance(maybe, dict):
            config = maybe
    enabled_default = bool(config.get("enabled_default", False))
    if override is not None:
        enabled = bool(override)
    else:
        enabled = enabled_default
    model_value = config.get("model_path") if isinstance(config, dict) else None
    if isinstance(model_value, str) and model_value.strip():
        candidate = Path(model_value.strip())
        if not candidate.is_absolute():
            model_path = WORKING_DIR_PATH / candidate
        else:
            model_path = candidate
    else:
        model_path = WORKING_DIR_PATH / "models" / "mobilenetv3-small.onnx"
    max_frames_value = config.get("max_video_frames", 2) if isinstance(config, dict) else 2
    try:
        max_frames = max(1, int(max_frames_value))
    except Exception:
        max_frames = 2
    prefer_ffmpeg = bool(config.get("prefer_ffmpeg", True)) if isinstance(config, dict) else True

    semantic_cfg = config.get("semantic") if isinstance(config.get("semantic"), dict) else {}
    semantic_enabled = bool((semantic_cfg or {}).get("enabled", True))
    semantic_model = str((semantic_cfg or {}).get("model", "ViT-B-32"))
    semantic_pretrained = str((semantic_cfg or {}).get("pretrained", "laion2b_s34b_b79k"))
    try:
        scene_threshold = float((semantic_cfg or {}).get("scene_threshold", 27.0))
    except (TypeError, ValueError):
        scene_threshold = 27.0
    try:
        scene_min_len = float((semantic_cfg or {}).get("scene_min_len", 15.0))
    except (TypeError, ValueError):
        scene_min_len = 15.0

    transcription_cfg = config.get("transcription") if isinstance(config.get("transcription"), dict) else {}
    transcription_enabled = bool((transcription_cfg or {}).get("enabled", True))
    transcription_model = str((transcription_cfg or {}).get("model", "base"))
    max_duration_value = (transcription_cfg or {}).get("max_duration")
    try:
        transcription_max_duration = (
            float(max_duration_value) if max_duration_value not in (None, "") else None
        )
    except (TypeError, ValueError):
        transcription_max_duration = None
    try:
        transcription_beam_size = max(1, int((transcription_cfg or {}).get("beam_size", 5)))
    except (TypeError, ValueError):
        transcription_beam_size = 5
    transcription_vad = bool((transcription_cfg or {}).get("vad_filter", False))

    caption_cfg = config.get("captions") if isinstance(config.get("captions"), dict) else {}
    caption_enabled = bool((caption_cfg or {}).get("enabled", False))
    caption_model = str((caption_cfg or {}).get("model", "Salesforce/blip-image-captioning-base"))
    try:
        caption_max_length = max(16, int((caption_cfg or {}).get("max_length", 64)))
    except (TypeError, ValueError):
        caption_max_length = 64

    ann_cfg = config.get("ann") if isinstance(config.get("ann"), dict) else {}
    ann_enabled = bool((ann_cfg or {}).get("enabled", True))
    ann_backend = str((ann_cfg or {}).get("backend", "auto"))

    return LightAnalysisSettings(
        enabled=enabled,
        model_path=model_path,
        max_video_frames=max_frames,
        prefer_ffmpeg=prefer_ffmpeg,
        semantic_enabled=semantic_enabled,
        semantic_model=semantic_model,
        semantic_pretrained=semantic_pretrained,
        scene_threshold=scene_threshold,
        scene_min_len=scene_min_len,
        transcription_enabled=transcription_enabled,
        transcription_model=transcription_model,
        transcription_max_duration=transcription_max_duration,
        transcription_beam_size=transcription_beam_size,
        transcription_vad=transcription_vad,
        caption_enabled=caption_enabled,
        caption_model=caption_model,
        caption_max_length=caption_max_length,
        ann_enabled=ann_enabled,
        ann_backend=ann_backend,
    )


def _resolve_gpu_settings(
    settings: Optional[Dict[str, object]], overrides: Optional[Dict[str, object]]
) -> GPUSettings:
    config: Dict[str, object] = {}
    if isinstance(settings, dict):
        maybe = settings.get("gpu")
        if isinstance(maybe, dict):
            config = maybe
    overrides = overrides or {}

    policy_value = overrides.get("policy", config.get("policy", "AUTO"))
    policy = str(policy_value or "AUTO").upper()
    if policy not in {"AUTO", "FORCE_GPU", "CPU_ONLY"}:
        policy = "AUTO"

    allow_value = overrides.get("allow_hwaccel_video")
    if allow_value is None:
        allow_hwaccel = bool(config.get("allow_hwaccel_video", True))
    else:
        allow_hwaccel = bool(allow_value)

    min_vram_value = overrides.get("min_free_vram_mb", config.get("min_free_vram_mb", 512))
    try:
        min_vram_mb = max(0, int(min_vram_value))
    except (TypeError, ValueError):
        min_vram_mb = 0

    max_workers_value = overrides.get("max_gpu_workers", config.get("max_gpu_workers", 2))
    try:
        max_gpu_workers = max(1, int(max_workers_value))
    except (TypeError, ValueError):
        max_gpu_workers = 1

    return GPUSettings(
        policy=policy,
        allow_hwaccel_video=allow_hwaccel,
        min_free_vram_mb=min_vram_mb,
        max_gpu_workers=max_gpu_workers,
    )


def _resolve_fingerprint_settings(
    settings: Optional[Dict[str, object]], overrides: Optional[Dict[str, object]]
) -> FingerprintSettings:
    config: Dict[str, object] = {}
    if isinstance(settings, dict):
        maybe = settings.get("fingerprints")
        if isinstance(maybe, dict):
            config = maybe
    overrides = overrides or {}

    def _bool(name: str, default: bool) -> bool:
        if name in overrides:
            return bool(overrides[name])
        return bool(config.get(name, default))

    def _float(name: str, default: float) -> float:
        value = overrides.get(name, config.get(name, default))
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _int(name: str, default: int, minimum: int = 1) -> int:
        value = overrides.get(name, config.get(name, default))
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        return max(minimum, result)

    def _path(name: str) -> Optional[Path]:
        value = overrides.get(name, config.get(name))
        if isinstance(value, str) and value.strip():
            candidate = Path(os.path.expanduser(value.strip()))
            if not candidate.is_absolute():
                return (WORKING_DIR_PATH / candidate).resolve()
            return candidate
        return None

    phase = overrides.get("phase_mode", config.get("phase_mode", "off"))
    if not isinstance(phase, str):
        phase = "off"
    phase = phase.lower()
    if phase not in {"off", "during-scan", "post-scan"}:
        phase = "off"

    return FingerprintSettings(
        enable_video_tmk=_bool("enable_video_tmk", False),
        enable_audio_chroma=_bool("enable_audio_chroma", False),
        enable_video_vhash_prefilter=_bool("enable_video_vhash_prefilter", True),
        phase_mode=phase,
        max_concurrency=_int("max_concurrency", 2, minimum=1),
        io_gentle_ms=_int("io_gentle_ms", 120, minimum=0),
        batch_size=_int("batch_size", 50, minimum=1),
        tmk_similarity_threshold=_float("tmk_similarity_threshold", 0.75),
        chroma_match_threshold=_float("chroma_match_threshold", 0.15),
        tmk_bin_path=_path("tmk_bin_path"),
        fpcalc_path=_path("fpcalc_path"),
    )


class ScanStateStore:
    def __init__(self, conn: sqlite3.Connection, drive_label: str, interval_seconds: int = 5):
        self.conn = conn
        self.drive_label = drive_label
        self.interval_seconds = max(1, int(interval_seconds))
        self._last_checkpoint = 0.0

    def _key(self, name: str) -> str:
        return f"{self.drive_label}::{name}"

    def load(self) -> Dict[str, str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT key, value FROM scan_state WHERE key LIKE ?",
            (f"{self.drive_label}::%",),
        )
        rows = cur.fetchall()
        state: Dict[str, str] = {}
        for key, value in rows:
            suffix = key.split("::", 1)[1] if "::" in key else key
            state[suffix] = value
        return state

    def clear(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM scan_state WHERE key LIKE ?",
            (f"{self.drive_label}::%",),
        )
        self.conn.commit()
        self._last_checkpoint = 0.0

    def checkpoint(self, phase: str, last_path: Optional[str], *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_checkpoint) < self.interval_seconds:
            return
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self.conn.cursor()
        rows = [
            (self._key("phase"), phase),
            (self._key("timestamp"), timestamp),
        ]
        if last_path is not None:
            rows.append((self._key("last_path_processed"), last_path))
        cur.executemany(
            "INSERT INTO scan_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            rows,
        )
        self.conn.commit()
        self._last_checkpoint = now


def run(cmd:list[str]) -> tuple[int,str,str]:
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
        out, err = p.communicate()
        return p.returncode, out, err
    except FileNotFoundError as exc:
        # Mirror the return signature while preventing the whole scan from
        # crashing when optional CLI dependencies (mediainfo/smartctl/ffmpeg)
        # are not installed.
        return 127, "", str(exc)

def mediainfo_json(file_path: str, executable: Optional[str]) -> Optional[dict]:
    if not executable:
        return None
    code, out, err = run([executable, "--Output=JSON", file_path])
    if code == 0 and out.strip():
        try:
            return json.loads(out)
        except Exception:
            return None
    return None

def ffmpeg_verify(file_path: str, executable: Optional[str]) -> bool:
    if not any(file_path.lower().endswith(e) for e in VIDEO_EXTS):
        return True
    if not executable:
        return True
    # -v error: only show errors; -xerror: stop on error
    code, out, err = run([executable, "-v", "error", "-xerror", "-i", file_path, "-f", "null", "-", "-nostdin"])
    return code == 0

def hash_blake3(
    file_path: str,
    chunk: int = 1024 * 1024,
    *,
    on_chunk: Optional[Callable[[int, float], None]] = None,
) -> str:
    if _blake3_hash is None:
        h = hashlib.sha256()
    else:
        h = _blake3_hash()
    with open(file_path, "rb") as f:
        while True:
            start = time.perf_counter()
            b = f.read(chunk)
            elapsed = time.perf_counter() - start
            if not b:
                if on_chunk is not None and elapsed > 0:
                    on_chunk(0, elapsed)
                break
            h.update(b)
            if on_chunk is not None:
                on_chunk(len(b), elapsed)
    return h.hexdigest()


def _iso_from_timestamp(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_av(path: str) -> bool:
    return Path(path).suffix.lower() in AV_EXTS


def _load_existing(
    conn: sqlite3.Connection, drive_label: str, *, casefold: bool
) -> Dict[str, dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, path, size_bytes, mtime_utc, deleted FROM files WHERE drive_label=?",
        (drive_label,),
    )
    existing: Dict[str, dict] = {}
    for row in cur.fetchall():
        file_id, path, size_bytes, mtime_utc, deleted = row
        key = key_for_path(path, casefold=casefold)
        existing[key] = {
            "id": file_id,
            "path": path,
            "size_bytes": size_bytes,
            "mtime_utc": mtime_utc,
            "deleted": int(deleted or 0),
        }
    return existing


def _mark_deleted(
    conn: sqlite3.Connection,
    drive_label: str,
    *,
    deleted_paths: Iterable[str],
) -> Tuple[int, List[str]]:
    cur = conn.cursor()
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    deleted_paths = list(deleted_paths)
    if not deleted_paths:
        return 0, []
    cur.executemany(
        "UPDATE files SET deleted=1, deleted_ts=? WHERE drive_label=? AND path=? AND deleted=0",
        [(timestamp, drive_label, path) for path in deleted_paths],
    )
    conn.commit()
    return cur.rowcount, deleted_paths[:5]


def _restore_active(conn: sqlite3.Connection, drive_label: str, paths: Iterable[str]) -> None:
    batch = [(drive_label, path) for path in paths]
    if not batch:
        return
    cur = conn.cursor()
    cur.executemany(
        "UPDATE files SET deleted=0, deleted_ts=NULL WHERE drive_label=? AND path=?",
        batch,
    )
    conn.commit()


def _upsert_file(
    conn: sqlite3.Connection,
    drive_label: str,
    info: FileInfo,
    *,
    hash_value: Optional[str],
    media_blob: Optional[str],
    integrity_ok: Optional[int],
) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM files WHERE drive_label=? AND path=?",
        (drive_label, info.path),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            """
            UPDATE files
            SET size_bytes=?, hash_blake3=?, media_json=?, integrity_ok=?, mtime_utc=?, deleted=0, deleted_ts=NULL
            WHERE id=?
            """,
            (
                int(info.size_bytes),
                hash_value,
                media_blob,
                integrity_ok,
                info.mtime_utc,
                int(row[0]),
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO files(
                drive_label, path, size_bytes, hash_blake3, media_json, integrity_ok, mtime_utc, deleted, deleted_ts
            )
            VALUES(?,?,?,?,?,?,?,?,NULL)
            """,
            (
                drive_label,
                info.path,
                int(info.size_bytes),
                hash_value,
                media_blob,
                integrity_ok,
                info.mtime_utc,
                0,
            ),
        )
    conn.commit()

def init_db(db_path: str):
    db_path_obj = Path(db_path)
    db_path_obj.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path_obj))
    c = conn.cursor()
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS drives(
        id INTEGER PRIMARY KEY,
        label TEXT NOT NULL,
        mount_path TEXT NOT NULL,
        total_bytes INTEGER,
        free_bytes INTEGER,
        smart_scan TEXT,      -- raw smartctl --scan/--xall best-effort
        scanned_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY,
        drive_label TEXT NOT NULL,
        path TEXT NOT NULL,
        size_bytes INTEGER,
        hash_blake3 TEXT,
        media_json TEXT,
        integrity_ok INTEGER,
        mtime_utc TEXT,
        deleted INTEGER DEFAULT 0,
        deleted_ts TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_files_drive ON files(drive_label);
    CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash_blake3);
    CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
    CREATE TABLE IF NOT EXISTS inventory(
        path TEXT PRIMARY KEY,
        size_bytes INTEGER NOT NULL,
        mtime_utc TEXT NOT NULL,
        ext TEXT,
        mime TEXT,
        category TEXT,
        drive_label TEXT,
        drive_type TEXT,
        indexed_utc TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_inventory_ext ON inventory(ext);
    CREATE INDEX IF NOT EXISTS idx_inventory_mime ON inventory(mime);
    CREATE INDEX IF NOT EXISTS idx_inventory_category ON inventory(category);
    CREATE INDEX IF NOT EXISTS idx_inventory_mtime ON inventory(mtime_utc);
    CREATE TABLE IF NOT EXISTS scan_state(
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_drives_label_unique ON drives(label);
    """)
    conn.commit()
    # Migrations for legacy shards
    c.execute("PRAGMA table_info(files)")
    existing_cols = {row[1] for row in c.fetchall()}
    for name, ddl in (
        ("deleted", "ALTER TABLE files ADD COLUMN deleted INTEGER DEFAULT 0"),
        ("deleted_ts", "ALTER TABLE files ADD COLUMN deleted_ts TEXT"),
    ):
        if name not in existing_cols:
            c.execute(ddl)
    conn.commit()
    ensure_features_table(conn)
    return conn


def ensure_catalog_inventory_tables(db_path: str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory_stats (
                id INTEGER PRIMARY KEY,
                scan_ts_utc TEXT NOT NULL,
                drive_label TEXT NOT NULL,
                total_files INTEGER NOT NULL,
                by_video INTEGER NOT NULL,
                by_audio INTEGER NOT NULL,
                by_image INTEGER NOT NULL,
                by_document INTEGER NOT NULL,
                by_archive INTEGER NOT NULL,
                by_executable INTEGER NOT NULL,
                by_other INTEGER NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_inventory_stats(
    db_path: str,
    *,
    drive_label: str,
    totals: Dict[str, int],
    total_files: int,
) -> None:
    ensure_catalog_inventory_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO inventory_stats(
                scan_ts_utc,
                drive_label,
                total_files,
                by_video,
                by_audio,
                by_image,
                by_document,
                by_archive,
                by_executable,
                by_other
            )
            VALUES(datetime('now'),?,?,?,?,?,?,?,?,?)
            """,
            (
                drive_label,
                int(total_files),
                int(totals.get("video", 0)),
                int(totals.get("audio", 0)),
                int(totals.get("image", 0)),
                int(totals.get("document", 0)),
                int(totals.get("archive", 0)),
                int(totals.get("executable", 0)),
                int(totals.get("other", 0)),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _iso_from_stat(stat_result: os.stat_result) -> str:
    return datetime.utcfromtimestamp(stat_result.st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")


def _inventory_scan(
    *,
    shard_conn: sqlite3.Connection,
    catalog_db_path: str,
    drive_label: str,
    drive_type: str,
    mount_path: Path,
    perf_config,
    robust_cfg,
    debug_slow: bool,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> Dict[str, object]:
    totals: Dict[str, int] = {
        "video": 0,
        "audio": 0,
        "image": 0,
        "document": 0,
        "archive": 0,
        "executable": 0,
        "other": 0,
    }
    metrics = {
        "dirs_scanned": 0,
        "files_seen": 0,
        "skipped_perm": 0,
        "skipped_toolong": 0,
        "skipped_ignored": 0,
    }

    start_time = time.perf_counter()
    writer = InventoryWriter(
        shard_conn,
        batch_size=max(1, int(getattr(robust_cfg, "batch_files", 1000))),
        flush_interval=max(0.5, float(getattr(robust_cfg, "batch_seconds", 2.0))),
    )

    try:
        root_display = normalize_path(str(mount_path))
        root_fs = to_fs_path(root_display, mode=robust_cfg.long_paths)
    except PathTooLongError:
        metrics["skipped_toolong"] += 1
        return {
            "total_files": 0,
            "totals": totals,
            "duration_seconds": 0.0,
            "skipped_perm": metrics["skipped_perm"],
            "skipped_toolong": metrics["skipped_toolong"],
            "skipped_ignored": metrics["skipped_ignored"],
        }

    queue: Deque[Tuple[str, str]] = deque()
    queue.append((root_display, root_fs))
    visited_dirs: set[Tuple[int, int]] = set()
    if robust_cfg.follow_symlinks:
        try:
            root_stat = os.stat(root_fs, follow_symlinks=True)
            visited_dirs.add((root_stat.st_dev, root_stat.st_ino))
        except OSError:
            pass

    progress_last_emit = 0.0

    def emit_progress(force: bool = False) -> None:
        nonlocal progress_last_emit
        now = time.monotonic()
        if not force and (now - progress_last_emit) < 5:
            return
        payload = {
            "type": "progress",
            "phase": "enumerating",
            "elapsed_s": int(now - start_time),
            "dirs_scanned": metrics["dirs_scanned"],
            "files_total": metrics["files_seen"],
            "files_seen": metrics["files_seen"],
            "av_seen": 0,
            "inventory_written": writer.total_written,
            "skipped_perm": metrics["skipped_perm"],
            "skipped_toolong": metrics["skipped_toolong"],
            "skipped_ignored": metrics["skipped_ignored"],
        }
        if progress_callback is not None:
            try:
                progress_callback(payload)
            except Exception:
                pass
        else:
            try:
                print(json.dumps(payload), flush=True)
            except Exception:
                pass
        progress_last_emit = now

    sleep_range = enumerate_sleep_range(perf_config.profile, bool(perf_config.gentle_io))

    while queue:
        display_dir, fs_dir = queue.popleft()
        try:
            iterator = os.scandir(fs_dir)
        except PermissionError:
            metrics["skipped_perm"] += 1
            continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            if is_transient(exc):
                continue
            LOGGER.debug("Inventory: failed to scandir %s: %s", fs_dir, exc)
            continue

        with iterator as entries:
            for entry in entries:
                fs_path = os.path.join(fs_dir, entry.name)
                display_path = normalize_path(from_fs_path(fs_path))

                try:
                    if entry.is_dir(follow_symlinks=robust_cfg.follow_symlinks):
                        if robust_cfg.skip_hidden and is_hidden(entry, fs_path=fs_path, display_path=display_path):
                            metrics["skipped_ignored"] += 1
                            continue
                        if should_ignore(display_path, patterns=robust_cfg.skip_globs):
                            metrics["skipped_ignored"] += 1
                            continue
                        if robust_cfg.follow_symlinks:
                            try:
                                stat_dir = entry.stat(follow_symlinks=True)
                                key = (stat_dir.st_dev, stat_dir.st_ino)
                                if key in visited_dirs:
                                    continue
                                visited_dirs.add(key)
                            except OSError:
                                pass
                        metrics["dirs_scanned"] += 1
                        queue.append((display_path, fs_path))
                        continue
                except PermissionError:
                    metrics["skipped_perm"] += 1
                    continue
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    if is_transient(exc):
                        continue
                    LOGGER.debug("Inventory: error inspecting %s: %s", fs_path, exc)
                    continue

                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue

                if robust_cfg.skip_hidden and is_hidden(entry, fs_path=fs_path, display_path=display_path):
                    metrics["skipped_ignored"] += 1
                    continue
                if should_ignore(display_path, patterns=robust_cfg.skip_globs):
                    metrics["skipped_ignored"] += 1
                    continue

                try:
                    stat_result = entry.stat(follow_symlinks=False)
                except PermissionError:
                    metrics["skipped_perm"] += 1
                    continue
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    if is_transient(exc):
                        continue
                    LOGGER.debug("Inventory: stat failed for %s: %s", fs_path, exc)
                    continue

                metrics["files_seen"] += 1
                mime, ext = detect_mime(fs_path)
                category = categorize(mime, ext)
                totals[category] = totals.get(category, 0) + 1
                indexed_utc = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                row = InventoryRow(
                    path=display_path,
                    size_bytes=int(stat_result.st_size),
                    mtime_utc=_iso_from_stat(stat_result),
                    ext=ext or None,
                    mime=mime,
                    category=category,
                    drive_label=drive_label,
                    drive_type=drive_type,
                    indexed_utc=indexed_utc,
                )
                writer.add(row)
                emit_progress()

        if sleep_range:
            time.sleep(random.uniform(*sleep_range))
        elif debug_slow:
            time.sleep(0.01)

    writer.flush(force=True)
    emit_progress(force=True)

    duration_seconds = time.perf_counter() - start_time
    total_files = metrics["files_seen"]
    record_inventory_stats(
        catalog_db_path,
        drive_label=drive_label,
        totals=totals,
        total_files=total_files,
    )
    return {
        "total_files": total_files,
        "duration_seconds": duration_seconds,
        "totals": totals,
        "skipped_perm": metrics["skipped_perm"],
        "skipped_toolong": metrics["skipped_toolong"],
        "skipped_ignored": metrics["skipped_ignored"],
        "inventory_written": writer.total_written,
    }

def try_smart_overview(executable: Optional[str]) -> Optional[str]:
    # Best-effort: capture smartctl --scan-open and a subset of PhysicalDrive outputs
    if not executable:
        return None
    acc = {"scan": None, "details": []}
    code, out, err = run([executable, "--scan-open"])
    if code == 0:
        acc["scan"] = out
    for n in range(0, 10):
        code, out, err = run([executable, "-i", "-H", "-A", "-j", fr"\\.\PhysicalDrive{n}"])
        if code == 0 and out.strip():
            acc["details"].append(json.loads(out))
    return json.dumps(acc, ensure_ascii=False)

def scan_drive(
    label: str,
    mount_path: str,
    catalog_db_path: str,
    *,
    shard_db_path: Optional[str] = None,
    inventory_only: bool = False,
    full_rescan: bool = False,
    resume: bool = True,
    checkpoint_seconds: int = 5,
    debug_slow: bool = False,
    settings: Optional[Dict[str, object]] = None,
    perf_overrides: Optional[Dict[str, object]] = None,
    fingerprint_overrides: Optional[Dict[str, object]] = None,
    robust_overrides: Optional[Dict[str, object]] = None,
    light_analysis: Optional[bool] = None,
    gpu_overrides: Optional[Dict[str, object]] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    mount = Path(mount_path)
    if not mount.exists():
        print(f"[ERROR] Mount path not found: {mount_path}")
        sys.exit(2)

    shard_path = Path(shard_db_path) if shard_db_path else get_shard_db_path(WORKING_DIR_PATH, label)

    effective_settings = settings or load_settings(WORKING_DIR_PATH)
    perf_overrides = perf_overrides or {}
    light_cfg = _resolve_light_analysis(effective_settings, light_analysis)
    fingerprint_cfg = _resolve_fingerprint_settings(effective_settings, fingerprint_overrides)
    gpu_cfg = _resolve_gpu_settings(effective_settings, gpu_overrides)
    perf_config = resolve_performance_config(
        str(mount),
        settings=effective_settings,
        cli_overrides=perf_overrides,
    )
    LOGGER.info(
        "Perf: profile=%s threads=%s chunk=%s ffmpeg_parallel=%s gentle_io=%s",
        perf_config.profile,
        perf_config.worker_threads,
        perf_config.hash_chunk_bytes,
        perf_config.ffmpeg_parallel,
        str(bool(perf_config.gentle_io)).lower(),
    )
    try:
        print(
            json.dumps(
                {
                    "type": "performance",
                    "profile": perf_config.profile,
                    "auto_profile": perf_config.auto_profile,
                    "source": perf_config.source,
                    "worker_threads": perf_config.worker_threads,
                    "hash_chunk_bytes": perf_config.hash_chunk_bytes,
                    "ffmpeg_parallel": perf_config.ffmpeg_parallel,
                    "gentle_io": bool(perf_config.gentle_io),
                }
            ),
            flush=True,
        )
    except Exception:
        pass

    LOGGER.info(
        "Delta rescan = %s, resume = %s, checkpoint = %ss",
        "false" if full_rescan else "true",
        "true" if resume else "false",
        int(checkpoint_seconds),
    )

    start_time = time.perf_counter()

    statuses = _refresh_tool_status()
    missing_required = [tool for tool in REQUIRED_TOOLS if not statuses.get(tool, {}).get("present")]
    if missing_required and not inventory_only:
        for tool in missing_required:
            print(json.dumps({"type": "tool_missing", "tool": tool}), flush=True)
            print(
                f"Required tool missing: {tool}. Please install or configure a portable path.",
                file=sys.stderr,
            )
        sys.exit(3)

    if not statuses.get("smartctl", {}).get("present"):
        LOGGER.warning("smartctl not found. SMART data will be skipped.")

    total, used, free = shutil.disk_usage(mount)
    conn = init_db(str(shard_path))
    light_pipeline: Optional[LightAnalysisPipeline] = None
    fingerprint_pipeline: Optional[FingerprintPipeline] = None
    if not inventory_only:
        light_pipeline = LightAnalysisPipeline(
            settings=light_cfg,
            gpu_settings=gpu_cfg,
            connection=conn,
            ffmpeg_path=TOOL_PATHS.get("ffmpeg"),
            perf_profile=str(perf_config.profile),
            progress_callback=progress_callback,
            start_time=start_time,
            drive_label=label,
        )
        light_pipeline.prepare()
        fingerprint_pipeline = FingerprintPipeline(
            settings=fingerprint_cfg,
            shard_path=shard_path,
            progress_callback=progress_callback,
            start_time=start_time,
            perf_profile=str(perf_config.profile),
            cancel_token=cancel_token,
        )
        fingerprint_pipeline.prepare(conn)
    conn.execute(
        """
        INSERT INTO drives(label, mount_path, total_bytes, free_bytes, smart_scan, scanned_at)
        VALUES(?,?,?,?,?,datetime('now'))
        ON CONFLICT(label) DO UPDATE SET
            mount_path=excluded.mount_path,
            total_bytes=excluded.total_bytes,
            free_bytes=excluded.free_bytes,
            smart_scan=excluded.smart_scan,
            scanned_at=excluded.scanned_at
        """,
        (
            label,
            str(mount.resolve()),
            int(total),
            int(free),
            try_smart_overview(TOOL_PATHS.get("smartctl")),
        ),
    )
    conn.commit()

    is_windows = os.name == "nt"
    robust_raw: Dict[str, object] = {}
    if isinstance(effective_settings, dict):
        maybe_robust = effective_settings.get("robust")
        if isinstance(maybe_robust, dict):
            robust_raw = maybe_robust
    robust_overrides = robust_overrides or {}
    robust_cfg = merge_settings(robust_raw, robust_overrides)
    robust_cfg.batch_seconds = clamp_batch_seconds(robust_cfg.batch_seconds, perf_config.profile)
    LOGGER.info("Robust settings: %s", robust_cfg.as_log_line())

    ignore_patterns: List[str] = []
    ignore_patterns.extend(list(robust_cfg.ignore))
    ignore_patterns.extend(list(robust_cfg.skip_globs))
    if ignore_patterns:
        seen_patterns: set[str] = set()
        ordered_patterns: List[str] = []
        for pattern in ignore_patterns:
            pattern = pattern.strip()
            if not pattern or pattern in seen_patterns:
                continue
            ordered_patterns.append(pattern)
            seen_patterns.add(pattern)
        ignore_patterns = ordered_patterns
    else:
        ignore_patterns = []

    if inventory_only:
        drive_type_value = str(perf_config.profile)
        result = _inventory_scan(
            shard_conn=conn,
            catalog_db_path=str(catalog_db_path),
            drive_label=label,
            drive_type=drive_type_value,
            mount_path=mount,
            perf_config=perf_config,
            robust_cfg=robust_cfg,
            debug_slow=debug_slow,
            progress_callback=progress_callback,
        )
        conn.close()
        return result

    state_store = ScanStateStore(conn, label, interval_seconds=int(checkpoint_seconds))
    if not resume:
        state_store.clear()
        resume_state: Dict[str, str] = {}
    else:
        resume_state = state_store.load()

    resume_path = resume_state.get("last_path_processed") if resume_state else None
    resume_key = key_for_path(resume_path, casefold=is_windows) if resume_path else None
    resume_consumed = not bool(resume_key)

    cancel_token = CancellationToken()

    metrics = {
        "dirs_scanned": 0,
        "files_seen": 0,
        "av_total": 0,
        "skipped_perm": 0,
        "skipped_toolong": 0,
        "skipped_ignored": 0,
        "retries": 0,
    }

    processed_files = 0
    processed_av = 0
    unchanged_count = 0
    total_enqueued = 0
    pending_tasks = 0
    last_processed_path = resume_path

    progress_last_emit = 0.0

    def _emit_progress(phase: str, *, force: bool = False) -> None:
        nonlocal progress_last_emit, last_processed_path
        now = time.monotonic()
        if not force and (now - progress_last_emit) < 5:
            return
        elapsed = int(now - start_time)
        payload = {
            "type": "progress",
            "phase": phase,
            "elapsed_s": elapsed,
            "dirs_scanned": metrics["dirs_scanned"],
            "files_total": metrics["files_seen"],
            "files_seen": processed_files,
            "av_seen": processed_av,
            "skipped_perm": metrics["skipped_perm"],
            "skipped_toolong": metrics["skipped_toolong"],
            "skipped_ignored": metrics["skipped_ignored"],
        }
        if metrics["av_total"]:
            payload["total_av"] = metrics["av_total"]
        if progress_callback is not None:
            try:
                progress_callback(payload)
            except Exception:
                pass
        else:
            try:
                print(json.dumps(payload), flush=True)
            except Exception:
                pass
        LOGGER.debug(
            "%s — dirs=%s files=%s processed=%s skipped=(perm=%s,long=%s,ignored=%s)",
            phase,
            metrics["dirs_scanned"],
            metrics["files_seen"],
            processed_files,
            metrics["skipped_perm"],
            metrics["skipped_toolong"],
            metrics["skipped_ignored"],
        )
        if resume:
            state_store.checkpoint(phase, last_processed_path if last_processed_path else None, force=force)
        progress_last_emit = now

    def _stat_path(path: str, *, follow_symlinks: bool) -> Optional[os.stat_result]:
        attempts = 0
        delay = 0.5
        while attempts < 3 and not cancel_token.is_set():
            attempts += 1
            try:
                start = time.monotonic()
                result = os.stat(path, follow_symlinks=follow_symlinks)
                elapsed = time.monotonic() - start
                if elapsed > robust_cfg.op_timeout_s:
                    raise TimeoutError(f"stat timeout after {elapsed:.1f}s")
                return result
            except PermissionError:
                metrics["skipped_perm"] += 1
                LOGGER.warning("Permission denied while stating %s", from_fs_path(path))
                return None
            except OSError as exc:
                if is_transient(exc) and attempts < 3:
                    metrics["retries"] += 1
                    time.sleep(min(2.0, delay))
                    delay *= 2
                    continue
                LOGGER.warning("stat failed for %s: %s", from_fs_path(path), exc)
                return None
        return None

    def _open_scandir(fs_path: str, display_path: str) -> Optional[os.ScandirIterator]:
        attempts = 0
        delay = 0.5
        while attempts < 3 and not cancel_token.is_set():
            attempts += 1
            try:
                start = time.monotonic()
                iterator = os.scandir(fs_path)
                elapsed = time.monotonic() - start
                if elapsed > robust_cfg.op_timeout_s:
                    iterator.close()
                    raise TimeoutError(f"scandir timeout after {elapsed:.1f}s")
                return iterator
            except PermissionError:
                metrics["skipped_perm"] += 1
                LOGGER.warning("Permission denied while enumerating %s", display_path)
                return None
            except OSError as exc:
                if is_transient(exc) and attempts < 3:
                    metrics["retries"] += 1
                    time.sleep(min(2.0, delay))
                    delay *= 2
                    continue
                LOGGER.warning("Failed to enumerate %s: %s", display_path, exc)
                return None
        LOGGER.warning("Giving up on %s after repeated failures", display_path)
        return None

    enumeration_sleep = enumerate_sleep_range(perf_config.profile, perf_config.gentle_io)

    base_sleep_range = None
    if perf_config.profile == "NETWORK":
        base_sleep_range = (0.002, 0.005)
    elif perf_config.gentle_io and perf_config.profile == "USB":
        base_sleep_range = (0.0015, 0.003)

    rate_controller = RateController(
        enabled=bool(perf_config.gentle_io or perf_config.profile == "NETWORK"),
        worker_threads=perf_config.worker_threads,
        base_sleep_range=base_sleep_range,
        latency_threshold=0.05 if perf_config.profile == "NETWORK" else 0.04,
    )
    ffmpeg_semaphore = threading.Semaphore(max(1, perf_config.ffmpeg_parallel))
    task_queue: "queue.Queue[object]" = queue.Queue(maxsize=max(1, int(robust_cfg.queue_max)))
    result_queue: "queue.Queue[WorkerResult]" = queue.Queue()
    sentinel = object()
    mediainfo_path = TOOL_PATHS.get("mediainfo")
    ffmpeg_path = TOOL_PATHS.get("ffmpeg")
    retry_delays = (0.1, 0.3, 0.9)

    existing_rows = _load_existing(conn, label, casefold=is_windows)
    if resume_key and resume_key not in existing_rows:
        resume_consumed = True

    restore_batch: List[str] = []
    pending_updates: List[Tuple[int, Optional[str], Optional[str], Optional[int], str, int]] = []
    pending_inserts: List[Tuple[str, str, int, Optional[str], Optional[str], Optional[int], str]] = []
    last_flush = time.monotonic()
    pragma_batches = 0

    def _flush_db(force: bool = False) -> None:
        nonlocal pending_updates, pending_inserts, restore_batch, last_flush, pragma_batches
        if not force:
            if (
                (len(pending_updates) + len(pending_inserts)) < robust_cfg.batch_files
                and (time.monotonic() - last_flush) < robust_cfg.batch_seconds
            ):
                return
        cur = conn.cursor()
        executed = False
        if pending_updates:
            cur.executemany(
                """
                UPDATE files
                SET size_bytes=?, hash_blake3=?, media_json=?, integrity_ok=?, mtime_utc=?, deleted=0, deleted_ts=NULL
                WHERE id=?
                """,
                pending_updates,
            )
            pending_updates = []
            executed = True
        if pending_inserts:
            cur.executemany(
                """
                INSERT INTO files(
                    drive_label, path, size_bytes, hash_blake3, media_json, integrity_ok, mtime_utc, deleted, deleted_ts
                )
                VALUES(?,?,?,?,?,?,?,0,NULL)
                """,
                pending_inserts,
            )
            pending_inserts = []
            executed = True
        if restore_batch:
            cur.executemany(
                "UPDATE files SET deleted=0, deleted_ts=NULL WHERE drive_label=? AND path=?",
                [(label, path) for path in restore_batch],
            )
            restore_batch = []
            executed = True
        if executed:
            conn.commit()
            pragma_batches += 1
            if pragma_batches % 25 == 0:
                try:
                    conn.execute("PRAGMA optimize")
                except sqlite3.Error:
                    pass
        if executed or force:
            last_flush = time.monotonic()

    def _drain_results(block: bool = False) -> None:
        nonlocal pending_tasks, processed_files, processed_av, last_processed_path
        timeout = 0.5 if block else 0.0
        while pending_tasks:
            try:
                result = result_queue.get(timeout=timeout)
            except queue.Empty:
                break
            pending_tasks -= 1
            info = result.info
            processed_files += 1
            if info.is_av:
                processed_av += 1
            last_processed_path = info.path
            if result.error_message:
                LOGGER.debug("Recorded warning for %s: %s", info.path, result.error_message)
            if info.existing_id:
                pending_updates.append(
                    (
                        int(info.size_bytes),
                        result.hash_value,
                        result.media_blob,
                        result.integrity_ok,
                        info.mtime_utc,
                        int(info.existing_id),
                    )
                )
            else:
                pending_inserts.append(
                    (
                        label,
                        info.path,
                        int(info.size_bytes),
                        result.hash_value,
                        result.media_blob,
                        result.integrity_ok,
                        info.mtime_utc,
                    )
                )
            if light_pipeline is not None:
                light_pipeline.process(info, metadata=result.media_metadata)
            if fingerprint_pipeline is not None:
                fingerprint_pipeline.submit(result)
            _flush_db(force=False)
            _emit_progress("hashing")

    def _process_file(info: FileInfo) -> WorkerResult:
        integrity_ok: Optional[int] = None if info.is_av else 1
        media_blob: Optional[str] = None
        hash_value: Optional[str] = None
        error_message: Optional[str] = None
        attempts = 0

        while not cancel_token.is_set():
            try:
                delay = rate_controller.before_task(task_queue.qsize())
                if delay > 0:
                    time.sleep(delay)

                def _on_chunk(bytes_read: int, elapsed: float) -> None:
                    if bytes_read > 0:
                        rate_controller.note_io(elapsed)

                hash_value = hash_blake3(
                    info.fs_path,
                    chunk=perf_config.hash_chunk_bytes,
                    on_chunk=_on_chunk,
                )
                metadata = mediainfo_json(info.fs_path, mediainfo_path) if info.is_av else None
                if metadata is not None:
                    media_blob = json.dumps(metadata, ensure_ascii=False)
                    integrity_ok = 0 if metadata.get("error") else 1
                else:
                    media_blob = None
                    integrity_ok = None if info.is_av else 1
                if info.is_av and not cancel_token.is_set():
                    with ffmpeg_semaphore:
                        ok = ffmpeg_verify(info.fs_path, ffmpeg_path)
                    if ok is False:
                        integrity_ok = 0
                rate_controller.note_success()
                error_message = None
                break
            except (OSError, IOError) as exc:
                rate_controller.note_error()
                attempts += 1
                if attempts < len(retry_delays):
                    time.sleep(retry_delays[attempts - 1])
                    continue
                LOGGER.warning("I/O error while processing %s: %s", info.path, exc)
                media_blob = json.dumps({"error": str(exc)}, ensure_ascii=False)
                integrity_ok = 0
                hash_value = None
                error_message = str(exc)
                break
            except Exception as exc:
                rate_controller.note_error()
                LOGGER.exception("Failed to process %s", info.path)
                media_blob = json.dumps({"error": str(exc)}, ensure_ascii=False)
                integrity_ok = 0
                hash_value = None
                error_message = str(exc)
                break
        else:
            error_message = "cancelled"

        return WorkerResult(
            info=info,
            hash_value=hash_value,
            media_blob=media_blob,
            integrity_ok=integrity_ok,
            error_message=error_message,
            media_metadata=metadata,
        )

    def _worker() -> None:
        while True:
            try:
                item = task_queue.get(timeout=0.5)
            except queue.Empty:
                if cancel_token.is_set():
                    continue
                continue
            try:
                if item is sentinel:
                    break
                assert isinstance(item, FileInfo)
                if cancel_token.is_set():
                    continue
                result_queue.put(_process_file(item))
            finally:
                task_queue.task_done()

    workers: List[threading.Thread] = []
    for _ in range(perf_config.worker_threads):
        thread = threading.Thread(target=_worker, name="scan-worker")
        thread.daemon = True
        thread.start()
        workers.append(thread)

    try:
        root_display = str(mount)
        try:
            root_fs = to_fs_path(root_display, mode=robust_cfg.long_paths)
        except PathTooLongError:
            metrics["skipped_toolong"] += 1
            LOGGER.error("Mount path too long: %s", root_display)
            cancel_token.set()
            root_fs = root_display

        stack: Deque[Tuple[str, str]] = deque()
        stack.append((root_display, root_fs))
        visited_dirs: set[Tuple[int, int]] = set()
        if robust_cfg.follow_symlinks:
            root_stat = _stat_path(root_fs, follow_symlinks=True)
            if root_stat:
                visited_dirs.add((root_stat.st_dev, root_stat.st_ino))

        while stack and not cancel_token.is_set():
            display_dir, fs_dir = stack.pop()
            iterator = _open_scandir(fs_dir, display_dir)
            if iterator is None:
                continue
            metrics["dirs_scanned"] += 1
            with iterator as it:
                for entry in it:
                    if cancel_token.is_set():
                        break
                    entry_fs = entry.path
                    display_entry = from_fs_path(entry_fs)
                    if robust_cfg.skip_hidden and is_hidden(entry, fs_path=entry_fs, display_path=display_entry):
                        metrics["skipped_ignored"] += 1
                        continue
                    if ignore_patterns and should_ignore(display_entry, patterns=ignore_patterns):
                        metrics["skipped_ignored"] += 1
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=robust_cfg.follow_symlinks):
                            try:
                                next_fs = to_fs_path(display_entry, mode=robust_cfg.long_paths)
                            except PathTooLongError:
                                metrics["skipped_toolong"] += 1
                                LOGGER.warning("Skipping long directory %s", display_entry)
                                continue
                            if robust_cfg.follow_symlinks:
                                dir_stat = _stat_path(next_fs, follow_symlinks=True)
                                if not dir_stat:
                                    continue
                                inode_key = (dir_stat.st_dev, dir_stat.st_ino)
                                if inode_key in visited_dirs:
                                    LOGGER.warning("Detected symlink loop at %s", display_entry)
                                    continue
                                visited_dirs.add(inode_key)
                            stack.append((display_entry, next_fs))
                            continue
                    except OSError:
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    stat_result = _stat_path(entry_fs, follow_symlinks=False)
                    if stat_result is None:
                        continue
                    try:
                        fs_file = to_fs_path(display_entry, mode=robust_cfg.long_paths)
                    except PathTooLongError:
                        metrics["skipped_toolong"] += 1
                        LOGGER.warning("Skipping long path %s", display_entry)
                        continue
                    info = FileInfo(
                        path=display_entry,
                        fs_path=fs_file,
                        size_bytes=int(stat_result.st_size),
                        mtime_utc=_iso_from_timestamp(stat_result.st_mtime),
                        is_av=_is_av(display_entry),
                    )
                    metrics["files_seen"] += 1
                    if info.is_av:
                        metrics["av_total"] += 1
                    existing_key = key_for_path(info.path, casefold=is_windows)
                    existing_row = existing_rows.pop(existing_key, None)
                    if existing_row is not None:
                        info.existing_id = existing_row["id"]
                        info.was_deleted = bool(existing_row["deleted"])
                        if (
                            not full_rescan
                            and not info.was_deleted
                            and int(existing_row["size_bytes"]) == info.size_bytes
                            and existing_row["mtime_utc"] == info.mtime_utc
                        ):
                            restore_batch.append(existing_row["path"])
                            unchanged_count += 1
                            if len(restore_batch) >= 2000:
                                _flush_db(force=True)
                            continue
                    if resume and not resume_consumed:
                        processed_files += 1
                        if info.is_av:
                            processed_av += 1
                        last_processed_path = info.path
                        if resume_key == existing_key:
                            resume_consumed = True
                        continue
                    while not cancel_token.is_set():
                        try:
                            task_queue.put(info, timeout=0.5)
                            pending_tasks += 1
                            total_enqueued += 1
                            break
                        except queue.Full:
                            _drain_results(block=True)
                            _flush_db(force=False)
                    if cancel_token.is_set():
                        break
                    if enumeration_sleep:
                        time.sleep(random.uniform(*enumeration_sleep))
                    if debug_slow:
                        time.sleep(0.01)
                    _drain_results(block=False)
                    _emit_progress("enumerating")
    except KeyboardInterrupt:
        cancel_token.set()
        LOGGER.warning("Scan cancelled by user.")
    except Exception as exc:
        cancel_token.set()
        LOGGER.exception("Enumeration failure: %s", exc)

    _emit_progress("enumerating", force=True)

    for _ in workers:
        while True:
            try:
                task_queue.put(sentinel, timeout=0.5)
                break
            except queue.Full:
                _drain_results(block=True)
                _flush_db(force=False)

    while pending_tasks:
        _drain_results(block=True)
    _flush_db(force=True)

    task_queue.join()
    for thread in workers:
        thread.join()

    _emit_progress("hashing", force=True)

    deleted_count = 0
    deleted_examples: List[str] = []
    if not cancel_token.is_set():
        stale_paths = [row["path"] for row in existing_rows.values()]
        deleted_count, deleted_examples = _mark_deleted(conn, label, deleted_paths=stale_paths)
        if deleted_count:
            LOGGER.info(
                "Marked %s files as deleted (examples: %s)",
                deleted_count,
                ", ".join(deleted_examples) if deleted_examples else "—",
            )

    if resume:
        state_store.checkpoint("hashing", last_processed_path, force=True)
        state_store.checkpoint("finalizing", None, force=True)
        state_store.clear()

    duration = time.perf_counter() - start_time
    LOGGER.info(
        "Scan complete for %s. Processed %s files (%s AV) in %.2fs.",
        label,
        processed_files,
        processed_av,
        duration,
    )
    LOGGER.info(
        "Skipped: perm=%s, long=%s, ignored=%s (retries=%s)",
        metrics["skipped_perm"],
        metrics["skipped_toolong"],
        metrics["skipped_ignored"],
        metrics["retries"],
    )
    light_summary: Optional[Dict[str, object]] = None
    fingerprint_summary: Optional[Dict[str, object]] = None
    if light_pipeline is not None:
        light_summary = light_pipeline.finalize()
    if fingerprint_pipeline is not None:
        fingerprint_pipeline.flush()
        fingerprint_summary = fingerprint_pipeline.finalize()
    conn.close()
    return {
        "total_files": metrics["files_seen"],
        "av_files": metrics["av_total"],
        "pending": total_enqueued,
        "unchanged": unchanged_count,
        "deleted": deleted_count,
        "duration_seconds": duration,
        "performance": perf_config.as_dict(),
        "skipped_perm": metrics["skipped_perm"],
        "skipped_toolong": metrics["skipped_toolong"],
        "skipped_ignored": metrics["skipped_ignored"],
        "light_analysis": light_summary,
        "fingerprints": fingerprint_summary,
    }



def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan a drive and populate the VideoCatalog databases."
    )
    parser.add_argument("--label", help="Drive label to record in the catalog.")
    parser.add_argument(
        "--mount",
        dest="mount_path",
        help="Filesystem path where the drive is mounted.",
    )
    parser.add_argument(
        "--catalog-db",
        dest="catalog_db",
        help="Optional path to the catalog database (defaults to working dir).",
    )
    parser.add_argument(
        "--shard-db",
        dest="shard_db",
        help="Optional path to the shard database (defaults to working dir).",
    )
    parser.add_argument(
        "--structure-scan",
        action="store_true",
        help="Profile folder structure and compute confidence scores without hashing.",
    )
    parser.add_argument(
        "--structure-verify",
        action="store_true",
        help="Augment structure profiling with external verification sources.",
    )
    parser.add_argument(
        "--structure-export-review",
        metavar="PATH",
        help="Export low-confidence review queue to the given JSON path.",
    )
    parser.add_argument(
        "--maint-target",
        dest="maint_target",
        help="Maintenance target: catalog, shard:<label>, or all-shards.",
    )
    parser.add_argument(
        "--maint-action",
        choices=["quick", "integrity", "full", "vacuum", "backup"],
        help="Run database maintenance instead of scanning.",
    )
    parser.add_argument(
        "--maint-force",
        action="store_true",
        help="Force VACUUM during maintenance, overriding free-space thresholds.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging output.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug helpers such as slow enumeration sleeps.",
    )
    parser.add_argument(
        "--debug-slow-enumeration",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--batch-files",
        type=int,
        help="Batch database commits after this many files (default: 1000).",
    )
    parser.add_argument(
        "--batch-seconds",
        type=float,
        help="Maximum seconds between batched commits (default: 2).",
    )
    parser.add_argument(
        "--queue-max",
        type=int,
        help="Maximum work items queued ahead of hashing (default: 10000).",
    )
    parser.add_argument(
        "--skip-hidden",
        action="store_true",
        help="Skip hidden/system files and directories during enumeration.",
    )
    parser.add_argument(
        "--skip-glob",
        action="append",
        metavar="PATTERN",
        help="Glob pattern to ignore (repeatable).",
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinks and junctions; cycles are detected and skipped.",
    )
    parser.add_argument(
        "--long-paths",
        choices=["auto", "force", "off"],
        help="Control Windows extended-length path handling (default: auto).",
    )
    parser.add_argument(
        "--op-timeout",
        type=int,
        help="Filesystem operation timeout in seconds before retry (default: 30).",
    )
    parser.add_argument(
        "--full-rescan",
        action="store_true",
        help="Force a full rescan of all files, ignoring the delta optimizer.",
    )
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="Enumerate files without hashing and populate the lightweight inventory table.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start a new scan even if a checkpoint is present.",
    )
    parser.add_argument(
        "--checkpoint-seconds",
        type=int,
        default=5,
        help="Seconds between resume checkpoints (default: 5).",
    )
    parser.add_argument(
        "--perf-profile",
        choices=["AUTO", "SSD", "HDD", "USB", "NETWORK"],
        help="Override automatic performance profile detection.",
    )
    parser.add_argument(
        "--perf-threads",
        type=int,
        help="Override worker thread count.",
    )
    parser.add_argument(
        "--gpu-policy",
        choices=["AUTO", "FORCE_GPU", "CPU_ONLY"],
        help="Select GPU inference policy for ONNX models.",
    )
    parser.add_argument(
        "--gpu-hwaccel",
        dest="gpu_hwaccel",
        action="store_true",
        help="Enable FFmpeg CUDA/NVDEC acceleration when available.",
    )
    parser.add_argument(
        "--no-gpu-hwaccel",
        dest="gpu_hwaccel",
        action="store_false",
        help="Disable FFmpeg hardware acceleration.",
    )
    parser.set_defaults(gpu_hwaccel=None)
    parser.add_argument(
        "--perf-chunk",
        type=int,
        help="Override hash chunk size in bytes.",
    )
    parser.add_argument(
        "--perf-ffmpeg",
        type=int,
        help="Override FFmpeg parallelism.",
    )
    parser.add_argument(
        "--perf-gentle-io",
        dest="perf_gentle_io",
        action="store_const",
        const=True,
        help="Force-enable gentle I/O throttling.",
    )
    parser.add_argument(
        "--no-perf-gentle-io",
        dest="perf_gentle_io",
        action="store_const",
        const=False,
        help="Disable gentle I/O throttling even if recommended.",
    )
    parser.set_defaults(perf_gentle_io=None)
    parser.add_argument(
        "--export-csv",
        nargs="?",
        const="",
        help="Export results to CSV after scanning. Optional path overrides the default.",
    )
    parser.add_argument(
        "--export-jsonl",
        nargs="?",
        const="",
        help="Export results to JSONL after scanning. Optional path overrides the default.",
    )
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="Include rows marked as deleted in exports.",
    )
    parser.add_argument(
        "--av-only",
        action="store_true",
        help="Only include audio/video files in exports.",
    )
    parser.add_argument(
        "--since",
        dest="since",
        help="Only include files updated at or after the given ISO 8601 UTC timestamp.",
    )
    parser.add_argument(
        "--light-analysis",
        dest="light_analysis",
        action="store_true",
        help="Enable lightweight embeddings for images and sampled video frames.",
    )
    parser.add_argument(
        "--no-light-analysis",
        dest="light_analysis",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(light_analysis=None)
    parser.add_argument(
        "--fingerprint-video-tmk",
        action="store_true",
        help="Enable TMK+PDQF video fingerprinting during this scan.",
    )
    parser.add_argument(
        "--fingerprint-audio-chroma",
        action="store_true",
        help="Enable Chromaprint audio fingerprinting during this scan.",
    )
    parser.add_argument(
        "--fingerprint-prefilter-vhash",
        dest="fingerprint_prefilter_vhash",
        action="store_true",
        help="Enable videohash prefilter when fingerprinting videos.",
    )
    parser.add_argument(
        "--no-prefilter-vhash",
        dest="fingerprint_prefilter_vhash",
        action="store_false",
        help="Disable videohash prefilter even if enabled in settings.",
    )
    parser.set_defaults(fingerprint_prefilter_vhash=None)
    parser.add_argument(
        "--fingerprint-phase",
        choices=["during-scan", "post-scan", "off"],
        help="Choose when to compute fingerprints (default from settings).",
    )
    parser.add_argument(
        "--fingerprint-workers",
        type=int,
        help="Override fingerprint worker count.",
    )
    parser.add_argument(
        "--fingerprint-io-ms",
        type=int,
        help="Override fingerprint I/O pacing in milliseconds between files.",
    )
    parser.add_argument(
        "--fingerprint-batch-size",
        type=int,
        help="Override fingerprint batch size per commit.",
    )
    parser.add_argument(
        "--tmk-bin",
        dest="fingerprint_tmk_bin",
        help="Path to TMK binaries if not on PATH.",
    )
    parser.add_argument(
        "--fpcalc",
        dest="fingerprint_fpcalc",
        help="Path to Chromaprint fpcalc executable.",
    )
    parser.add_argument(
        "--semantic-index",
        choices=["build", "rebuild"],
        help="Run semantic index maintenance instead of scanning.",
    )
    parser.add_argument(
        "--semantic-query",
        metavar="QUERY",
        help="Execute a semantic search query and print results.",
    )
    parser.add_argument(
        "--hybrid",
        dest="semantic_hybrid",
        action="store_true",
        help="Combine ANN and FTS scores when using --semantic-query.",
    )
    parser.add_argument(
        "--transcribe",
        dest="semantic_transcribe",
        action="store_true",
        help="Run the semantic transcription helper on indexed rows.",
    )
    parser.add_argument(
        "positional",
        nargs="*",
        help=argparse.SUPPRESS,
    )

    namespace = parser.parse_args(argv)
    positional = list(namespace.positional)

    if namespace.label is None and positional:
        namespace.label = positional.pop(0)
    if namespace.mount_path is None and positional:
        namespace.mount_path = positional.pop(0)
    if namespace.catalog_db is None and positional:
        namespace.catalog_db = positional.pop(0)

    if namespace.maint_action:
        namespace.positional = positional
        return namespace

    if namespace.label is None or namespace.mount_path is None:
        parser.error("Both --label and --mount are required.")

    namespace.positional = positional
    return namespace


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        LOGGER.error("Failed to create directory %s: %s", path, exc)
        sys.exit(4)


def _format_duration(seconds: float) -> str:
    total_seconds = int(round(max(0.0, float(seconds))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _format_bytes(value: int) -> str:
    if value <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    remaining = float(value)
    for unit in units:
        if remaining < 1024.0 or unit == units[-1]:
            return f"{remaining:.1f} {unit}" if unit != "B" else f"{int(remaining)} {unit}"
        remaining /= 1024.0
    return f"{remaining:.1f} PB"


def _format_filter_summary(filters: ExportFilters) -> str:
    since = filters.since_utc or "—"
    include = "true" if filters.include_deleted else "false"
    av_only = "true" if filters.av_only else "false"
    return f"include_deleted={include}, av_only={av_only}, since={since}"


def _resolve_maintenance_targets(
    target_spec: Optional[str], catalog_db_path: Path
) -> List[Dict[str, object]]:
    spec = (target_spec or "catalog").strip()
    lower = spec.lower()
    targets: List[Dict[str, object]] = []
    if lower in {"catalog", "catalog.db"}:
        targets.append({"kind": "catalog", "label": "catalog", "path": catalog_db_path})
        return targets
    if lower in {"all-shards", "all"}:
        shards_dir = get_shards_dir(WORKING_DIR_PATH)
        if shards_dir.exists():
            for shard_path in sorted(shards_dir.glob("*.db")):
                targets.append(
                    {
                        "kind": "shard",
                        "label": shard_path.stem,
                        "path": shard_path,
                    }
                )
        return targets
    if lower.startswith("shard:"):
        label = spec.split(":", 1)[1]
    else:
        label = spec
    candidate_path = Path(spec)
    if candidate_path.exists() and candidate_path.is_file():
        targets.append({"kind": "shard", "label": candidate_path.stem, "path": candidate_path})
        return targets
    shard_path = get_shard_db_path(WORKING_DIR_PATH, label)
    targets.append({"kind": "shard", "label": label, "path": shard_path})
    return targets


def _execute_maintenance_action(
    action: str,
    target: Dict[str, object],
    options: MaintenanceOptions,
    *,
    force: bool,
) -> Dict[str, object]:
    path = Path(target["path"])
    label = str(target["label"])
    kind = str(target.get("kind") or "shard")
    display = "Catalog" if kind == "catalog" else f"Shard {label}"
    safe = safe_label(label)
    result: Dict[str, object] = {"line": ""}
    if action == "integrity":
        integrity = check_integrity(path, busy_timeout_ms=options.busy_timeout_ms)
        ok = bool(integrity.get("ok"))
        issues = integrity.get("issues") or []
        summary = "OK" if ok else "FAILED"
        line = f"{display}: Integrity {summary}"
        if issues and not ok:
            sample = "; ".join(str(item) for item in list(issues)[:3])
            line += f" — sample: {sample}"
        result.update({"line": line, "integrity_ok": ok})
        update_maintenance_metadata(
            last_run=datetime.utcnow(),
            last_integrity_ok=ok,
            working_dir=WORKING_DIR_PATH,
        )
        return result

    if action == "backup":
        backup = light_backup(path, f"manual_{safe}", working_dir=WORKING_DIR_PATH, busy_timeout_ms=options.busy_timeout_ms)
        result["line"] = f"{display}: Backup stored at {backup}"
        update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
        return result

    if action == "quick":
        backup = light_backup(path, f"quick_{safe}", working_dir=WORKING_DIR_PATH, busy_timeout_ms=options.busy_timeout_ms)
        metrics = reindex_and_analyze(path, busy_timeout_ms=options.busy_timeout_ms)
        duration = metrics.get("duration_s")
        indexes = metrics.get("indexes_after")
        duration_text = f"{float(duration):.2f}s" if isinstance(duration, (int, float)) else "—"
        result["line"] = (
            f"{display}: Quick optimize complete (indexes={indexes}, duration={duration_text}, backup={backup})"
        )
        update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
        return result

    if action == "full":
        backup = light_backup(path, f"full_{safe}", working_dir=WORKING_DIR_PATH, busy_timeout_ms=options.busy_timeout_ms)
        metrics = reindex_and_analyze(path, busy_timeout_ms=options.busy_timeout_ms)
        vacuum_result = vacuum_if_needed(
            path,
            thresholds={"vacuum_free_bytes_min": options.vacuum_free_bytes_min},
            busy_timeout_ms=options.busy_timeout_ms,
            force=force,
        )
        reclaimed = int(vacuum_result.get("reclaimed_bytes") or 0)
        result.update(
            {
                "line": (
                    f"{display}: Full maintenance complete (indexes={metrics.get('indexes_after')}, "
                    f"reclaimed={_format_bytes(reclaimed)}, backup={backup})"
                ),
                "reclaimed_bytes": reclaimed,
            }
        )
        update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
        return result

    if action == "vacuum":
        backup = light_backup(path, f"vacuum_{safe}", working_dir=WORKING_DIR_PATH, busy_timeout_ms=options.busy_timeout_ms)
        vacuum_result = vacuum_if_needed(
            path,
            thresholds={"vacuum_free_bytes_min": options.vacuum_free_bytes_min},
            busy_timeout_ms=options.busy_timeout_ms,
            force=force,
        )
        if vacuum_result.get("skipped"):
            line = f"{display}: VACUUM skipped (threshold not met). Backup at {backup}"
            reclaimed = 0
        else:
            reclaimed = int(vacuum_result.get("reclaimed_bytes") or 0)
            line = f"{display}: VACUUM reclaimed {_format_bytes(reclaimed)} (backup={backup})"
        result.update({"line": line, "reclaimed_bytes": reclaimed})
        update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
        return result

    raise ValueError(f"Unsupported maintenance action: {action}")


def _run_cli_semantic(
    args: argparse.Namespace, settings: Dict[str, object]
) -> Optional[int]:
    semantic_index = getattr(args, "semantic_index", None)
    semantic_query = getattr(args, "semantic_query", None)
    semantic_transcribe = bool(getattr(args, "semantic_transcribe", False))
    operations = [bool(semantic_index), bool(semantic_query), semantic_transcribe]
    if not any(operations):
        return None
    if sum(1 for flag in operations if flag) > 1:
        message = "Choose only one of --semantic-index, --semantic-query, or --transcribe."
        LOGGER.error(message)
        print(f"[ERROR] {message}", file=sys.stderr)
        return 2
    config = SemanticConfig.from_settings(WORKING_DIR_PATH, settings)
    try:
        if semantic_index:
            rebuild = semantic_index == "rebuild"
            indexer = SemanticIndexer(config)
            stats = indexer.build(rebuild=rebuild)
            action = "Rebuild" if rebuild else "Build"
            print(
                f"Semantic index {action.lower()} complete — "
                f"processed {stats.get('processed', 0)} rows across {stats.get('shards', 0)} shards."
            )
            return 0
        if semantic_query:
            searcher = SemanticSearcher(config)
            mode = "hybrid" if getattr(args, "semantic_hybrid", False) else "ann"
            results, total = searcher.search(
                semantic_query,
                limit=25,
                offset=0,
                drive_label=getattr(args, "label", None),
                mode=mode,
                hybrid=bool(getattr(args, "semantic_hybrid", False)),
            )
            if not results:
                print(f"No semantic matches found for '{semantic_query}'.")
                return 0
            print(
                f"Semantic results for '{semantic_query}' (mode={mode}) — "
                f"showing {len(results)} of {total} entries:"
            )
            for row in results:
                metadata = row.get("metadata") if isinstance(row, dict) else {}
                summary = ""
                if isinstance(metadata, dict):
                    summary = str(
                        metadata.get("category")
                        or metadata.get("mime")
                        or metadata.get("extension")
                        or ""
                    )
                snippet = row.get("snippet") if isinstance(row, dict) else None
                line = (
                    f"[{row.get('rank', '?')}] {row.get('score', 0):.3f} "
                    f"{row.get('drive_label', '')} :: {row.get('path', '')}"
                )
                if summary:
                    line += f" ({summary})"
                if snippet:
                    line += f" — {snippet}"
                print(line)
            if total > len(results):
                print(
                    "… additional results truncated; use the API for pagination or increase limits."
                )
            return 0
        if semantic_transcribe:
            transcriber = SemanticTranscriber(config)
            result = transcriber.run()
            print(
                f"Semantic transcription complete — "
                f"updated {int(result.get('transcribed', 0))} records."
            )
            return 0
    except SemanticPhaseError as exc:
        LOGGER.error("%s", exc)
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    return 0


def _run_cli_maintenance(args: argparse.Namespace, catalog_db_path: Path) -> int:
    targets = _resolve_maintenance_targets(args.maint_target, catalog_db_path)
    if not targets:
        LOGGER.error("No maintenance targets resolved.")
        print("[ERROR] No maintenance targets resolved.", file=sys.stderr)
        return 1
    options = resolve_options(load_settings(WORKING_DIR_PATH))
    action = args.maint_action
    force = bool(getattr(args, "maint_force", False))
    total_reclaimed = 0
    exit_code = 0
    for target in targets:
        path = Path(target["path"])
        if not path.exists():
            LOGGER.warning("Maintenance target missing: %s", path)
            print(f"[WARN] Target not found: {path}", file=sys.stderr)
            exit_code = max(exit_code, 1)
            continue
        try:
            summary = _execute_maintenance_action(action, target, options, force=force)
        except Exception as exc:
            LOGGER.error("Maintenance failed for %s: %s", target["label"], exc)
            print(f"[ERROR] Maintenance failed for {target['label']}: {exc}", file=sys.stderr)
            return 5
        print(summary.get("line", ""))
        reclaimed = int(summary.get("reclaimed_bytes") or 0)
        total_reclaimed += reclaimed
        if summary.get("integrity_ok") is False:
            exit_code = max(exit_code, 6)
    if action in {"vacuum", "full"} and total_reclaimed > 0:
        print(f"Total reclaimed: {_format_bytes(total_reclaimed)}")
    return exit_code


def _run_structure_cli(
    args: argparse.Namespace,
    settings_data: Dict[str, object],
    catalog_db_path: Path,
    shard_db_path: Path,
    mount_path: Optional[Path],
) -> int:
    structure_settings = load_structure_config(settings_data)
    if not structure_settings.enable:
        LOGGER.info("Structure profiling disabled via settings.json (structure.enable=false)")
        return 0
    if not args.label:
        LOGGER.error("Structure operations require --label to resolve shard DBs.")
        print("[ERROR] --label is required for structure profiling.", file=sys.stderr)
        return 2
    do_scan = bool(args.structure_scan or args.structure_verify)
    if do_scan and mount_path is None:
        LOGGER.error("Structure scan requires --mount to locate folders.")
        print("[ERROR] --mount is required when running structure scans.", file=sys.stderr)
        return 2
    active_mount = mount_path or Path(args.mount_path or ".")
    if do_scan and not active_mount.exists():
        LOGGER.warning("Mount path %s does not exist; proceeding may skip folders.", active_mount)
    conn = connect(shard_db_path, read_only=False, check_same_thread=False)
    try:
        profiler = StructureProfiler(
            conn,
            settings=structure_settings,
            drive_label=args.label,
            mount_path=active_mount,
        )
        if do_scan:
            summary = profiler.profile(verify_external=bool(args.structure_verify))
            LOGGER.info(
                "Structure profiling complete — processed=%s, confident=%s, medium=%s, low=%s",
                summary.processed,
                summary.confident,
                summary.medium,
                summary.low,
            )
            print(
                "Structure profiling complete — processed="
                f"{summary.processed} (confident={summary.confident}, medium={summary.medium}, low={summary.low})"
            )
        if args.structure_export_review:
            destination = Path(args.structure_export_review)
            export_info = profiler.export_review(destination)
            LOGGER.info(
                "Exported %s review entries to %s",
                export_info.get("exported", 0),
                export_info.get("path"),
            )
            print(
                f"Review queue exported to {export_info.get('path')} "
                f"({int(export_info.get('exported', 0))} entries)."
            )
        return 0
    except Exception as exc:
        LOGGER.error("Structure profiling failed: %s", exc)
        print(f"[ERROR] Structure profiling failed: {exc}", file=sys.stderr)
        return 4
    finally:
        conn.close()


def _auto_optimize_after_scan(shard_path: Path, label: str, options: MaintenanceOptions) -> None:
    if not shard_path.exists():
        return
    try:
        LOGGER.info("Auto optimize shard %s", label)
        quick_optimize(shard_path, busy_timeout_ms=options.busy_timeout_ms)
        if options.auto_vacuum_after_scan:
            backup = light_backup(
                shard_path,
                f"auto_{safe_label(label)}",
                working_dir=WORKING_DIR_PATH,
                busy_timeout_ms=options.busy_timeout_ms,
            )
            LOGGER.info("Auto vacuum backup stored at %s", backup)
            vacuum_result = vacuum_if_needed(
                shard_path,
                thresholds={"vacuum_free_bytes_min": options.vacuum_free_bytes_min},
                busy_timeout_ms=options.busy_timeout_ms,
                force=False,
                active_check=lambda: False,
            )
            if vacuum_result.get("skipped"):
                LOGGER.info(
                    "Auto vacuum skipped for %s (reason=%s)",
                    label,
                    vacuum_result.get("reason") or "threshold",
                )
            else:
                reclaimed = int(vacuum_result.get("reclaimed_bytes") or 0)
                LOGGER.info(
                    "Auto vacuum reclaimed %s for %s",
                    _format_bytes(reclaimed),
                    label,
                )
        update_maintenance_metadata(last_run=datetime.utcnow(), working_dir=WORKING_DIR_PATH)
    except Exception as exc:
        LOGGER.warning("Auto maintenance failed for %s: %s", label, exc)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    settings_data = load_settings(WORKING_DIR_PATH)
    semantic_exit = _run_cli_semantic(args, settings_data)
    if semantic_exit is not None:
        return semantic_exit

    catalog_db_path = (
        _expand_user_path(args.catalog_db)
        if args.catalog_db
        else Path(DEFAULT_DB_PATH)
    )
    shard_db_path = (
        _expand_user_path(args.shard_db)
        if args.shard_db
        else get_shard_db_path(WORKING_DIR_PATH, args.label)
    )

    _ensure_directory(catalog_db_path.parent)
    _ensure_directory(shard_db_path.parent)

    startup_info = (
        f"Working directory: {WORKING_DIR_PATH} | "
        f"catalog: {catalog_db_path} | shard: {shard_db_path}"
    )
    LOGGER.info(startup_info)

    structure_requested = bool(
        args.structure_scan or args.structure_verify or args.structure_export_review
    )
    if structure_requested:
        mount_path = _expand_user_path(args.mount_path) if args.mount_path else None
        return _run_structure_cli(
            args,
            settings_data,
            Path(catalog_db_path),
            Path(shard_db_path),
            mount_path,
        )

    if args.maint_action:
        return _run_cli_maintenance(args, catalog_db_path)

    try:
        since_value = parse_since(getattr(args, "since", None))
    except ValueError as exc:
        LOGGER.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 2

    filters = ExportFilters(
        include_deleted=bool(getattr(args, "include_deleted", False)),
        av_only=bool(getattr(args, "av_only", False)),
        since_utc=since_value,
    )

    debug_flag = bool(args.debug or args.debug_slow_enumeration)
    checkpoint_seconds = max(1, int(getattr(args, "checkpoint_seconds", 5)))
    perf_cli_overrides: Dict[str, object] = {}
    if getattr(args, "perf_profile", None) is not None:
        perf_cli_overrides["profile"] = args.perf_profile
    if getattr(args, "perf_threads", None) is not None:
        perf_cli_overrides["worker_threads"] = args.perf_threads
    if getattr(args, "perf_chunk", None) is not None:
        perf_cli_overrides["hash_chunk_bytes"] = args.perf_chunk
    if getattr(args, "perf_ffmpeg", None) is not None:
        perf_cli_overrides["ffmpeg_parallel"] = args.perf_ffmpeg
    if getattr(args, "perf_gentle_io", None) is not None:
        perf_cli_overrides["gentle_io"] = args.perf_gentle_io

    robust_cli_overrides: Dict[str, object] = {}
    if getattr(args, "batch_files", None) is not None:
        robust_cli_overrides["batch_files"] = args.batch_files
    if getattr(args, "batch_seconds", None) is not None:
        robust_cli_overrides["batch_seconds"] = args.batch_seconds
    if getattr(args, "queue_max", None) is not None:
        robust_cli_overrides["queue_max"] = args.queue_max
    if getattr(args, "skip_hidden", False):
        robust_cli_overrides["skip_hidden"] = True
    if getattr(args, "skip_glob", None):
        robust_cli_overrides["skip_globs"] = args.skip_glob
    if getattr(args, "follow_symlinks", False):
        robust_cli_overrides["follow_symlinks"] = True
    if getattr(args, "long_paths", None) is not None:
        robust_cli_overrides["long_paths"] = args.long_paths
    if getattr(args, "op_timeout", None) is not None:
        robust_cli_overrides["op_timeout_s"] = args.op_timeout

    fingerprint_cli_overrides: Dict[str, object] = {}
    if getattr(args, "fingerprint_video_tmk", False):
        fingerprint_cli_overrides["enable_video_tmk"] = True
    if getattr(args, "fingerprint_audio_chroma", False):
        fingerprint_cli_overrides["enable_audio_chroma"] = True
    pref_value = getattr(args, "fingerprint_prefilter_vhash", None)
    if pref_value is not None:
        fingerprint_cli_overrides["enable_video_vhash_prefilter"] = pref_value
    if getattr(args, "fingerprint_phase", None):
        fingerprint_cli_overrides["phase_mode"] = args.fingerprint_phase
    if getattr(args, "fingerprint_workers", None) is not None:
        fingerprint_cli_overrides["max_concurrency"] = args.fingerprint_workers
    if getattr(args, "fingerprint_io_ms", None) is not None:
        fingerprint_cli_overrides["io_gentle_ms"] = args.fingerprint_io_ms
    if getattr(args, "fingerprint_batch_size", None) is not None:
        fingerprint_cli_overrides["batch_size"] = args.fingerprint_batch_size
    if getattr(args, "fingerprint_tmk_bin", None):
        fingerprint_cli_overrides["tmk_bin_path"] = args.fingerprint_tmk_bin
    if getattr(args, "fingerprint_fpcalc", None):
        fingerprint_cli_overrides["fpcalc_path"] = args.fingerprint_fpcalc

    gpu_cli_overrides: Dict[str, object] = {}
    if getattr(args, "gpu_policy", None):
        gpu_cli_overrides["policy"] = args.gpu_policy
    if getattr(args, "gpu_hwaccel", None) is not None:
        gpu_cli_overrides["allow_hwaccel_video"] = bool(args.gpu_hwaccel)

    result = scan_drive(
        args.label,
        args.mount_path,
        str(catalog_db_path),
        shard_db_path=str(shard_db_path),
        inventory_only=bool(getattr(args, "inventory_only", False)),
        full_rescan=bool(getattr(args, "full_rescan", False)),
        resume=not bool(getattr(args, "no_resume", False)),
        checkpoint_seconds=checkpoint_seconds,
        debug_slow=debug_flag,
        settings=settings_data,
        perf_overrides=perf_cli_overrides,
        fingerprint_overrides=fingerprint_cli_overrides,
        robust_overrides=robust_cli_overrides,
        light_analysis=getattr(args, "light_analysis", None),
        gpu_overrides=gpu_cli_overrides,
    )

    total_files = int(result.get("total_files", 0)) if isinstance(result, dict) else 0
    duration_seconds = float(result.get("duration_seconds", 0.0)) if isinstance(result, dict) else 0.0
    if getattr(args, "inventory_only", False):
        totals = result.get("totals", {}) if isinstance(result, dict) else {}
        summary_line = (
            "Inventory done — total files: "
            f"{total_files:,} — video: {int(totals.get('video', 0)):,} — "
            f"audio: {int(totals.get('audio', 0)):,} — image: {int(totals.get('image', 0)):,} "
            f"— duration: {_format_duration(duration_seconds)}"
        )
        light_line = None
    else:
        av_files = int(result.get("av_files", 0)) if isinstance(result, dict) else 0
        summary_line = (
            "Done — total files: "
            f"{total_files:,} — AV files: {av_files:,} — duration: {_format_duration(duration_seconds)}"
        )
        light_line = None
        fingerprint_line = None
        light_info = result.get("light_analysis") if isinstance(result, dict) else None
        if isinstance(light_info, dict):
            status = str(light_info.get("status") or "").lower()
            if status == "ok":
                light_line = (
                    "Light analysis — images: "
                    f"{int(light_info.get('images', 0)):,}, videos: {int(light_info.get('videos', 0)):,}, "
                    f"avg dim: {int(light_info.get('avg_dim', 0))}"
                )
                provider = str(light_info.get("provider") or "CPU")
                label_map = {
                    "CUDAExecutionProvider": "CUDA",
                    "DmlExecutionProvider": "DML",
                    "CPU": "CPU",
                }
                light_line += f", provider: {label_map.get(provider, provider)}"
                hwaccel = "on" if light_info.get("hwaccel") else "off"
                light_line += f", hwaccel: {hwaccel}"
                if light_info.get("warning"):
                    light_line += f" (warning: {light_info.get('warning')})"
            elif status in {"error", "skipped"} and light_info.get("message"):
                light_line = f"Light analysis skipped — {light_info.get('message')}"
        fp_info = result.get("fingerprints") if isinstance(result, dict) else None
        if isinstance(fp_info, dict):
            status = str(fp_info.get("status") or "").lower()
            if status == "ok":
                fingerprint_line = (
                    "Fingerprints — video: "
                    f"{int(fp_info.get('video', 0)):,}, audio: {int(fp_info.get('audio', 0)):,}, "
                    f"prefilter: {int(fp_info.get('vhash', 0)):,}"
                )
                if fp_info.get("errors"):
                    fingerprint_line += " (warnings: " + ", ".join(
                        str(err) for err in fp_info.get("errors", [])
                    ) + ")"
            elif fp_info.get("errors"):
                fingerprint_line = "Fingerprints skipped — " + "; ".join(
                    str(err) for err in fp_info.get("errors", [])
                )
            elif status == "skipped":
                fingerprint_line = "Fingerprints skipped — disabled"

    export_requests: list[tuple[str, Optional[str]]] = []
    if getattr(args, "export_csv", None) is not None:
        export_requests.append(("csv", args.export_csv))
    if getattr(args, "export_jsonl", None) is not None:
        export_requests.append(("jsonl", args.export_jsonl))

    if not getattr(args, "inventory_only", False):
        for fmt, override in export_requests:
            override_path = None if not override else _expand_user_path(override)
            target_display = str(override_path) if override_path else "default"
            LOGGER.info(
                "%s export starting → %s (%s)",
                fmt.upper(),
                target_display,
                _format_filter_summary(filters),
            )
            try:
                export_result = export_catalog(
                    catalog_db_path,
                    WORKING_DIR_PATH,
                    args.label,
                    filters,
                    fmt=fmt,
                    output_path=override_path,
                )
            except Exception as exc:
                LOGGER.error("%s export failed: %s", fmt.upper(), exc)
                print(f"[ERROR] {fmt.upper()} export failed: {exc}", file=sys.stderr)
                print(summary_line)
                return 5
            LOGGER.info(
                "%s export finished: %s (%s rows)",
                fmt.upper(),
                export_result.path,
                export_result.rows,
            )

    print(summary_line)
    if not getattr(args, "inventory_only", False):
        if light_line:
            print(light_line)
        if fingerprint_line:
            print(fingerprint_line)
    if not getattr(args, "inventory_only", False):
        try:
            maintenance_options = resolve_options(settings_data)
            _auto_optimize_after_scan(Path(shard_db_path), args.label, maintenance_options)
        except Exception:
            LOGGER.debug("Auto maintenance skipped due to configuration error.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
