"""Video thumbnail sampling for lightweight embeddings."""
from __future__ import annotations

import math
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

try:  # pragma: no cover - optional dependency guard
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    cv2 = None  # type: ignore

from .image_embed import ImageEmbedder


@dataclass(frozen=True)
class VideoFrameSample:
    timestamp: float
    vector: np.ndarray


class VideoThumbnailAnalyzer:
    """Best-effort video frame sampler that keeps I/O gentle."""

    def __init__(
        self,
        *,
        embedder: ImageEmbedder,
        ffmpeg_path: Optional[str],
        prefer_ffmpeg: bool = True,
        max_frames: int = 2,
        rate_limit_profile: Optional[str] = None,
        hwaccel_args: Optional[Sequence[str]] = None,
    ) -> None:
        self._embedder = embedder
        self._ffmpeg_path = Path(ffmpeg_path) if ffmpeg_path else None
        self._prefer_ffmpeg = bool(prefer_ffmpeg and self._ffmpeg_path)
        self._max_frames = max(1, int(max_frames))
        self._profile = (rate_limit_profile or "").upper()
        if self._profile in {"NETWORK", "USB"}:
            self._max_frames = min(self._max_frames, 1)
        self._hwaccel_args = list(hwaccel_args or [])

    def extract_features(self, video_path: Path) -> Tuple[Optional[np.ndarray], int]:
        vectors: List[np.ndarray] = []
        frames_collected = 0
        if self._prefer_ffmpeg:
            vectors, frames_collected = self._sample_with_ffmpeg(video_path)
        if not vectors and self._ffmpeg_path and not self._prefer_ffmpeg:
            vectors, frames_collected = self._sample_with_ffmpeg(video_path)
        if not vectors and cv2 is not None:
            vectors, frames_collected = self._sample_with_cv(video_path)
        if not vectors:
            return None, 0
        matrix = np.stack(vectors, axis=0)
        pooled = np.mean(matrix, axis=0)
        norm = np.linalg.norm(pooled)
        if norm > 0:
            pooled = pooled / norm
        if self._profile in {"NETWORK", "USB"}:
            time.sleep(0.02)
        return pooled.astype(np.float32, copy=False), frames_collected

    def _sample_with_ffmpeg(self, video_path: Path) -> Tuple[List[np.ndarray], int]:
        if not self._ffmpeg_path:
            return [], 0
        timestamps = self._candidate_timestamps(video_path)
        vectors: List[np.ndarray] = []
        used = 0
        for ts in timestamps:
            frame_bytes = self._extract_frame_ffmpeg(video_path, ts)
            if not frame_bytes:
                continue
            vec = self._embedder.embed_bytes(frame_bytes)
            if vec is None:
                continue
            vectors.append(vec)
            used += 1
            if len(vectors) >= self._max_frames:
                break
        return vectors, used

    def _candidate_timestamps(self, video_path: Path) -> Sequence[float]:
        duration = self._probe_duration(video_path)
        if duration and duration > 0:
            start = max(0.0, duration * 0.05)
            middle = max(0.0, duration * 0.5)
            candidates = [start]
            if self._max_frames > 1 and abs(middle - start) > 0.01:
                candidates.append(middle)
        else:
            candidates = [0.0]
            if self._max_frames > 1:
                candidates.append(1.0)
        dedup: List[float] = []
        seen = set()
        for value in candidates:
            key = round(value, 2)
            if key in seen:
                continue
            seen.add(key)
            dedup.append(value)
        return dedup

    def _probe_duration(self, video_path: Path) -> Optional[float]:
        if not self._ffmpeg_path:
            return None
        ffmpeg = self._ffmpeg_path
        suffix = ffmpeg.suffix
        probe_name = "ffprobe" + suffix
        probe_path = ffmpeg.with_name(probe_name)
        if not probe_path.exists():
            return None
        try:
            completed = subprocess.run(
                [str(probe_path), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(video_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except Exception:
            return None
        output = (completed.stdout or "").strip()
        if not output:
            return None
        try:
            value = float(output.splitlines()[0])
        except (ValueError, IndexError):
            return None
        if math.isfinite(value) and value > 0:
            return value
        return None

    def _extract_frame_ffmpeg(self, video_path: Path, timestamp: float) -> Optional[bytes]:
        if not self._ffmpeg_path:
            return None
        args = [str(self._ffmpeg_path), "-hide_banner", "-loglevel", "error"]
        if self._hwaccel_args:
            args.extend(self._hwaccel_args)
        if timestamp > 0:
            args.extend(["-ss", f"{timestamp:.3f}"])
        args.extend(["-i", str(video_path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"])
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                check=False,
                timeout=15,
            )
        except Exception:
            return None
        if completed.returncode != 0 or not completed.stdout:
            return None
        return completed.stdout

    def _sample_with_cv(self, video_path: Path) -> Tuple[List[np.ndarray], int]:
        if cv2 is None:
            return [], 0
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            cap.release()
            return [], 0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(frame_count) if frame_count and frame_count > 0 else 0
        indexes: List[int]
        if total_frames > 0:
            start_idx = max(0, int(total_frames * 0.05))
            mid_idx = max(0, int(total_frames * 0.5))
            indexes = [start_idx]
            if self._max_frames > 1 and abs(mid_idx - start_idx) > 1:
                indexes.append(mid_idx)
        else:
            fallback = int(max(1, fps) * 1)
            indexes = [0]
            if self._max_frames > 1:
                indexes.append(fallback)
        vectors: List[np.ndarray] = []
        used = 0
        for idx in indexes:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            vec = self._embedder.embed_image(img)
            if vec is None:
                continue
            vectors.append(vec)
            used += 1
            if len(vectors) >= self._max_frames:
                break
        cap.release()
        return vectors, used
