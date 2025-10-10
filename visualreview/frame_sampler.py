"""Frame sampling helpers for manual visual review assets."""
from __future__ import annotations

import io
import logging
import math
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image

from gpu.runtime import get_hwaccel_args

try:  # pragma: no cover - optional dependency guard
    import av  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    av = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    from scenedetect import SceneManager, open_video  # type: ignore
    from scenedetect.detectors import ContentDetector  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    SceneManager = None  # type: ignore
    ContentDetector = None  # type: ignore
    open_video = None  # type: ignore

LOGGER = logging.getLogger("videocatalog.visualreview.frames")


@dataclass(slots=True)
class FrameSamplerConfig:
    """Runtime configuration for :class:`FrameSampler`."""

    ffmpeg_path: Optional[Path] = None
    prefer_pyav: bool = False
    max_frames: int = 9
    scene_threshold: float = 27.0
    min_scene_len: float = 24.0
    fallback_percentages: Sequence[float] = field(
        default_factory=lambda: (0.05, 0.20, 0.35, 0.50, 0.65, 0.80, 0.95)
    )
    cropdetect: bool = False
    cropdetect_frames: int = 12
    cropdetect_skip_seconds: float = 1.0
    cropdetect_round: int = 16
    hwaccel_policy: str = "AUTO"
    allow_hwaccel: bool = True

    def normalized_ffmpeg(self) -> Path:
        if self.ffmpeg_path:
            return Path(self.ffmpeg_path)
        return Path("ffmpeg")


@dataclass(slots=True)
class FrameSample:
    """In-memory representation of an extracted video frame."""

    timestamp: float
    image: Image.Image


class FrameSampler:
    """Scene-aware frame sampler with FFmpeg/PyAV backends."""

    def __init__(self, config: Optional[FrameSamplerConfig] = None) -> None:
        self._config = config or FrameSamplerConfig()
        self._max_frames = max(1, int(self._config.max_frames))
        self._ffmpeg_path = self._config.normalized_ffmpeg()
        self._prefer_pyav = bool(self._config.prefer_pyav and av is not None)
        self._percentages = [
            float(p)
            for p in self._config.fallback_percentages
            if isinstance(p, (int, float))
        ]
        if not self._percentages:
            self._percentages = [0.05, 0.50, 0.95]
        self._crop_filter: Optional[str] = None
        self._hwaccel_enabled = bool(self._config.allow_hwaccel)

    def sample(self, video_path: Path, *, max_frames: Optional[int] = None) -> List[FrameSample]:
        """Return a list of sampled frames ordered by timestamp."""

        frame_budget = max(1, int(max_frames or self._max_frames))
        timestamps = self._collect_timestamps(video_path, frame_budget)
        if not timestamps:
            LOGGER.debug("No timestamps determined for video: %s", video_path)
            return []
        if self._config.cropdetect:
            self._crop_filter = self._detect_crop_filter(video_path)
        samples: List[FrameSample] = []
        extractor_order = ["pyav", "ffmpeg"] if self._prefer_pyav else ["ffmpeg", "pyav"]
        for backend in extractor_order:
            samples = self._extract_frames(video_path, timestamps, backend)
            if samples:
                break
        if not samples:
            LOGGER.warning("Frame extraction failed for video: %s", video_path)
            return []
        samples.sort(key=lambda sample: sample.timestamp)
        if len(samples) > frame_budget:
            samples = samples[:frame_budget]
        return samples

    # ------------------------------------------------------------------
    # Timestamp collection helpers
    # ------------------------------------------------------------------
    def _collect_timestamps(self, video_path: Path, frame_budget: int) -> List[float]:
        scenes = self._detect_scenes(video_path)
        if scenes:
            timestamps = self._timestamps_from_scenes(scenes, frame_budget)
            if timestamps:
                return timestamps
        duration = self._probe_duration(video_path)
        if duration:
            values = [
                max(0.0, min(duration, duration * pct)) for pct in self._percentages
            ]
            if duration > 0 and frame_budget > 0:
                step = duration / float(frame_budget + 1)
                for idx in range(frame_budget):
                    pos = step * (idx + 1)
                    values.append(max(0.0, min(duration, pos)))
            dedup = self._deduplicate(values, frame_budget)
            if dedup:
                return dedup
        fallback = [float(idx) * 2.0 for idx in range(frame_budget)]
        return fallback[:frame_budget]

    def _detect_scenes(self, video_path: Path) -> Sequence[Tuple[float, float]]:
        if SceneManager is None or open_video is None or ContentDetector is None:
            return []
        try:
            video = open_video(str(video_path))
        except Exception:  # pragma: no cover - defensive
            return []
        manager = SceneManager()
        try:
            detector = ContentDetector(
                threshold=float(self._config.scene_threshold),
                min_scene_len=self._config.min_scene_len,
            )
        except Exception:  # pragma: no cover - fallback defaults
            detector = ContentDetector()
        manager.add_detector(detector)
        try:
            manager.detect_scenes(video)
            scene_list = manager.get_scene_list()
        except Exception:
            scene_list = []
        finally:
            try:
                video.close()
            except Exception:  # pragma: no cover - best effort
                pass
        result: List[Tuple[float, float]] = []
        for start_time, end_time in scene_list:
            start_seconds = _timecode_to_seconds(start_time.timecode)
            end_seconds = _timecode_to_seconds(end_time.timecode)
            if math.isfinite(start_seconds) and math.isfinite(end_seconds):
                result.append((max(0.0, start_seconds), max(0.0, end_seconds)))
        return result

    def _timestamps_from_scenes(
        self, scenes: Sequence[Tuple[float, float]], frame_budget: int
    ) -> List[float]:
        timestamps: List[float] = []
        for start, end in scenes:
            midpoint = start + (max(end - start, 0.0) / 2.0)
            timestamps.append(midpoint if midpoint > 0 else start)
            if len(timestamps) >= frame_budget:
                break
        if timestamps:
            return self._deduplicate(timestamps, frame_budget)
        starts = [scene[0] for scene in scenes[:frame_budget]]
        return self._deduplicate(starts, frame_budget)

    def _deduplicate(self, values: Iterable[float], frame_budget: int) -> List[float]:
        seen = set()
        ordered: List[float] = []
        for value in values:
            rounded = round(float(value), 2)
            if rounded in seen:
                continue
            seen.add(rounded)
            ordered.append(float(value))
            if len(ordered) >= frame_budget:
                break
        return ordered

    # ------------------------------------------------------------------
    # Frame extraction
    # ------------------------------------------------------------------
    def _extract_frames(
        self,
        video_path: Path,
        timestamps: Sequence[float],
        backend: str,
    ) -> List[FrameSample]:
        if backend == "pyav" and av is not None:
            return self._extract_with_pyav(video_path, timestamps)
        if backend == "ffmpeg":
            return self._extract_with_ffmpeg(video_path, timestamps)
        return []

    def _extract_with_pyav(
        self, video_path: Path, timestamps: Sequence[float]
    ) -> List[FrameSample]:
        if av is None:
            return []
        try:
            container = av.open(str(video_path))
        except Exception:
            return []
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            container.close()
            return []
        stream.codec_context.skip_frame = "NONKEY"
        samples: List[FrameSample] = []
        try:
            for ts in timestamps:
                frame = self._decode_pyav_frame(container, stream, ts)
                if frame is None:
                    continue
                image = frame.to_image()
                if self._crop_filter:
                    image = _apply_crop(image, self._crop_filter)
                samples.append(FrameSample(timestamp=float(ts), image=image))
                if len(samples) >= self._max_frames:
                    break
        finally:
            container.close()
        return samples

    def _decode_pyav_frame(self, container, stream, timestamp: float):  # type: ignore[no-untyped-def]
        seek_time = int(timestamp / stream.time_base) if stream.time_base else None
        if seek_time is not None:
            try:
                container.seek(seek_time, stream=stream, any_frame=False, backward=True)
            except Exception:
                pass
        target_sec = float(timestamp)
        best_frame = None
        min_delta = float("inf")
        try:
            for packet in container.demux(stream):
                for frame in packet.decode():
                    if frame.time is None:
                        continue
                    frame_sec = float(frame.time)
                    delta = abs(frame_sec - target_sec)
                    if delta < min_delta:
                        min_delta = delta
                        best_frame = frame
                        if delta <= 0.05:
                            return frame
                if min_delta <= 0.05:
                    break
        except Exception:
            return best_frame
        return best_frame

    def _extract_with_ffmpeg(
        self, video_path: Path, timestamps: Sequence[float]
    ) -> List[FrameSample]:
        hwaccel_args: List[str] = []
        if self._hwaccel_enabled:
            hwaccel_args = get_hwaccel_args(
                self._config.hwaccel_policy, allow_hwaccel=self._config.allow_hwaccel
            )
        samples: List[FrameSample] = []
        for ts in timestamps:
            image = self._extract_frame_ffmpeg(video_path, ts, hwaccel_args)
            if image is None and hwaccel_args:
                LOGGER.debug("Retrying frame extraction without hwaccel for %s", video_path)
                self._hwaccel_enabled = False
                image = self._extract_frame_ffmpeg(video_path, ts, [])
            if image is None:
                continue
            if self._crop_filter:
                image = _apply_crop(image, self._crop_filter)
            samples.append(FrameSample(timestamp=float(ts), image=image))
            if len(samples) >= self._max_frames:
                break
        return samples

    def _extract_frame_ffmpeg(
        self,
        video_path: Path,
        timestamp: float,
        hwaccel_args: Sequence[str],
    ) -> Optional[Image.Image]:
        args: List[str] = [str(self._ffmpeg_path), "-hide_banner", "-loglevel", "error"]
        args.extend(hwaccel_args)
        if timestamp > 0:
            args.extend(["-ss", f"{timestamp:.3f}"])
        args.extend(["-i", str(video_path), "-frames:v", "1"])
        if self._crop_filter:
            args.extend(["-vf", self._crop_filter])
        args.extend(["-f", "image2pipe", "-vcodec", "png", "pipe:1"])
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                check=False,
                timeout=20,
            )
        except Exception as exc:  # pragma: no cover - subprocess failure
            LOGGER.debug("FFmpeg invocation failed: %s", exc)
            return None
        if completed.returncode != 0 or not completed.stdout:
            return None
        try:
            with Image.open(io.BytesIO(completed.stdout)) as image:
                return image.convert("RGB")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _probe_duration(self, video_path: Path) -> Optional[float]:
        probe = self._ffmpeg_path.with_name(self._ffmpeg_path.name.replace("ffmpeg", "ffprobe"))
        if not probe.exists():
            return None
        args = [
            str(probe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "csv=p=0",
            str(video_path),
        ]
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except Exception:  # pragma: no cover - subprocess failure
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

    def _detect_crop_filter(self, video_path: Path) -> Optional[str]:
        ffmpeg = str(self._ffmpeg_path)
        args: List[str] = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{max(0.0, float(self._config.cropdetect_skip_seconds)):.2f}",
            "-i",
            str(video_path),
            "-frames:v",
            str(max(1, int(self._config.cropdetect_frames))),
            "-vf",
            f"cropdetect=24:{int(self._config.cropdetect_round)}:0",
            "-f",
            "null",
            "-",
        ]
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        except Exception:
            return None
        logs = (completed.stderr or "") + "\n" + (completed.stdout or "")
        crop_value: Optional[str] = None
        for line in logs.splitlines():
            line = line.strip()
            if "crop=" not in line:
                continue
            marker = line.split("crop=")[-1]
            fields = marker.split()[0]
            if _valid_crop_filter(fields):
                crop_value = fields
        if crop_value:
            LOGGER.debug("Detected crop filter %s for %s", crop_value, video_path)
        return crop_value


def _valid_crop_filter(text: str) -> bool:
    parts = text.split(":")
    if len(parts) != 4:
        return False
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return False
    return all(value >= 0 for value in values) and values[0] > 0 and values[1] > 0


def _apply_crop(image: Image.Image, crop_filter: str) -> Image.Image:
    parts = crop_filter.split(":")
    if len(parts) != 4:
        return image
    try:
        width, height, x_off, y_off = (int(part) for part in parts)
    except ValueError:
        return image
    box = (x_off, y_off, x_off + width, y_off + height)
    try:
        return image.crop(box)
    except Exception:
        return image


def _timecode_to_seconds(timecode: str) -> float:
    try:
        hours, minutes, seconds = timecode.split(":")
    except ValueError:
        return 0.0
    try:
        sec_fraction = float(seconds)
    except ValueError:
        try:
            sec_fraction = float(Fraction(seconds))
        except Exception:
            sec_fraction = 0.0
    try:
        total = (
            int(hours) * 3600
            + int(minutes) * 60
            + sec_fraction
        )
        return float(total)
    except ValueError:
        return float(sec_fraction)
