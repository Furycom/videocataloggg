"""Semantic embedding helpers powered by OpenCLIP."""
from __future__ import annotations

import io
import json
import logging
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from gpu.capabilities import probe_gpu

LOGGER = logging.getLogger("videocatalog.semantic")

try:  # pragma: no cover - optional dependency guard
    import torch
except Exception:  # pragma: no cover - optional dependency guard
    torch = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    import open_clip
except Exception:  # pragma: no cover - optional dependency guard
    open_clip = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector
except Exception:  # pragma: no cover - optional dependency guard
    SceneManager = None  # type: ignore
    ContentDetector = None  # type: ignore
    open_video = None  # type: ignore


class SemanticModelError(RuntimeError):
    """Raised when the semantic embedding pipeline cannot be prepared."""


@dataclass(frozen=True)
class SemanticAnalyzerConfig:
    model_name: str = "ViT-B-32"
    pretrained: str = "laion2b_s34b_b79k"
    device_policy: str = "AUTO"
    ffmpeg_path: Optional[Path] = None
    max_video_frames: int = 4
    scene_threshold: float = 27.0
    min_scene_len: float = 15.0  # frames


class SemanticAnalyzer:
    """Wrapper around OpenCLIP models with optional PySceneDetect sampling."""

    def __init__(self, config: SemanticAnalyzerConfig) -> None:
        if open_clip is None or torch is None:
            raise SemanticModelError(
                "open_clip or torch is not available. Install open-clip-torch and torch."
            )
        self._config = config
        self._device = self._select_device(config.device_policy)
        try:
            self._model, _, self._preprocess = open_clip.create_model_and_transforms(
                config.model_name,
                pretrained=config.pretrained,
                device=self._device,
            )
        except Exception as exc:  # pragma: no cover - import guard
            raise SemanticModelError(f"Failed to load OpenCLIP model: {exc}") from exc
        try:
            tokenizer = open_clip.get_tokenizer(config.model_name)
        except Exception:
            tokenizer = open_clip.get_tokenizer("ViT-B-32")
        self._tokenizer = tokenizer
        self._max_frames = max(1, int(config.max_video_frames))
        self._ffmpeg_path = config.ffmpeg_path
        self._scene_threshold = float(config.scene_threshold)
        self._min_scene_len = max(1.0, float(config.min_scene_len))

    @staticmethod
    def _select_device(policy: str) -> str:
        normalized = (policy or "AUTO").upper()
        if torch is None:
            return "cpu"
        if normalized == "CPU_ONLY":
            return "cpu"
        caps = probe_gpu()
        if normalized == "FORCE_GPU" and torch.cuda.is_available():
            return "cuda"
        if caps.get("has_nvidia") and torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():  # type: ignore[attr-defined]
            return "mps"
        return "cpu"

    @property
    def device(self) -> str:
        return self._device

    def encode_image_path(self, image_path: Path) -> Optional[np.ndarray]:
        try:
            with Image.open(image_path) as image:
                return self.encode_image(image)
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def encode_image(self, image: Image.Image) -> Optional[np.ndarray]:
        try:
            processed = self._preprocess(image).unsqueeze(0)
        except Exception:
            return None
        if torch is None:
            return None
        tensor = processed.to(self._device)
        with torch.no_grad():
            embeddings = self._model.encode_image(tensor)
        return self._finalize_vector(embeddings)

    def encode_video(self, video_path: Path) -> Tuple[Optional[np.ndarray], int]:
        timestamps = self._collect_timestamps(video_path)
        vectors: List[np.ndarray] = []
        for timestamp in timestamps:
            frame_bytes = self._extract_frame(video_path, timestamp)
            if not frame_bytes:
                continue
            try:
                with Image.open(io.BytesIO(frame_bytes)) as image:
                    vector = self.encode_image(image)
            except OSError:
                vector = None
            if vector is None:
                continue
            vectors.append(vector)
            if len(vectors) >= self._max_frames:
                break
        if not vectors:
            return None, 0
        matrix = np.stack(vectors, axis=0)
        pooled = np.mean(matrix, axis=0)
        norm = np.linalg.norm(pooled)
        if norm > 0:
            pooled = pooled / norm
        return pooled.astype(np.float32, copy=False), len(vectors)

    def _collect_timestamps(self, video_path: Path) -> Sequence[float]:
        scenes = self._detect_scenes(video_path)
        if scenes:
            return self._timestamps_from_scenes(scenes)
        return self._fallback_timestamps(video_path)

    def _timestamps_from_scenes(self, scenes: Sequence[Tuple[float, float]]) -> Sequence[float]:
        timestamps: List[float] = []
        for start, end in scenes:
            midpoint = start + (max(end - start, 0.0) / 2.0)
            if midpoint < 0:
                midpoint = start
            timestamps.append(midpoint)
            if len(timestamps) >= self._max_frames:
                break
        if timestamps:
            return timestamps
        return [scene[0] for scene in scenes[: self._max_frames]]

    def _fallback_timestamps(self, video_path: Path) -> Sequence[float]:
        duration = self._probe_duration(video_path)
        if duration and duration > 0:
            step = duration / max(1, self._max_frames)
            values = [min(duration, step * (idx + 0.5)) for idx in range(self._max_frames)]
            return values
        return [float(idx) for idx in range(self._max_frames)]

    def _detect_scenes(self, video_path: Path) -> Sequence[Tuple[float, float]]:
        if SceneManager is None or open_video is None or ContentDetector is None:
            return []
        try:
            video = open_video(str(video_path))
        except Exception:
            return []
        scene_manager = SceneManager()
        try:
            detector = ContentDetector(threshold=self._scene_threshold, min_scene_len=self._min_scene_len)
        except Exception:
            detector = ContentDetector()
        scene_manager.add_detector(detector)
        try:
            scene_manager.detect_scenes(video)
            scene_list = scene_manager.get_scene_list()
        except Exception:
            scene_list = []
        finally:
            try:
                video.close()
            except Exception:
                pass
        result: List[Tuple[float, float]] = []
        for start_time, end_time in scene_list:
            try:
                start = float(start_time.get_seconds())
                end = float(end_time.get_seconds())
            except Exception:
                continue
            if math.isfinite(start) and math.isfinite(end):
                result.append((start, end))
        return result

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
                [str(probe_path), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video_path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return None
        if completed.returncode != 0:
            return None
        try:
            payload = json.loads(completed.stdout or "")
            duration = float(payload.get("format", {}).get("duration", 0.0))
            if math.isfinite(duration) and duration > 0:
                return duration
        except Exception:
            return None
        return None

    def _extract_frame(self, video_path: Path, timestamp: float) -> Optional[bytes]:
        if not self._ffmpeg_path:
            return None
        args: List[str] = [str(self._ffmpeg_path), "-hide_banner", "-loglevel", "error"]
        if timestamp > 0:
            args.extend(["-ss", f"{timestamp:.3f}"])
        args.extend(["-i", str(video_path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"])
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                check=False,
                timeout=20,
            )
        except Exception:
            return None
        if completed.returncode != 0 or not completed.stdout:
            return None
        return completed.stdout

    def _finalize_vector(self, embeddings: "torch.Tensor") -> Optional[np.ndarray]:  # type: ignore[name-defined]
        if torch is None:
            return None
        try:
            normalized = torch.nn.functional.normalize(embeddings, p=2, dim=-1)
            vector = normalized.detach().cpu().numpy().astype(np.float32)
        except Exception:
            return None
        if vector.ndim == 2 and vector.shape[0] == 1:
            vector = vector[0]
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        return vector.astype(np.float32, copy=False)


__all__ = [
    "SemanticAnalyzer",
    "SemanticAnalyzerConfig",
    "SemanticModelError",
]
