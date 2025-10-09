"""Transcription helpers powered by faster-whisper."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from gpu.capabilities import probe_gpu

LOGGER = logging.getLogger("videocatalog.transcribe")

try:  # pragma: no cover - optional dependency guard
    from faster_whisper import WhisperModel  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    WhisperModel = None  # type: ignore


@dataclass(frozen=True)
class TranscriptionConfig:
    enabled: bool = True
    model_size: str = "base"
    device_policy: str = "AUTO"
    max_duration: Optional[float] = None
    beam_size: int = 5
    vad_filter: bool = False


class TranscriptionService:
    """Faster-whisper transcription manager with GPU fallbacks."""

    def __init__(self, config: TranscriptionConfig) -> None:
        self._config = config
        self._model: Optional[WhisperModel] = None
        self._device: str = "cpu"
        self._compute_type: str = "int8"
        self._available = WhisperModel is not None and bool(config.enabled)
        self._load_attempted = False
        self._error: Optional[str] = None
        self._force_cpu = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self) -> Optional[str]:
        return self._error

    def _resolve_device(self) -> Tuple[str, str]:
        policy = (self._config.device_policy or "AUTO").upper()
        if self._force_cpu:
            policy = "CPU_ONLY"
        caps = probe_gpu()
        if policy != "CPU_ONLY" and caps.get("has_nvidia") and caps.get("cuda_available"):
            return "cuda", "float16"
        if caps.get("has_nvidia") and policy == "FORCE_GPU":
            return "cuda", "float16"
        return "cpu", "int8_float16"

    def _load_model(self) -> bool:
        if not self._available or self._load_attempted:
            return self._model is not None
        self._load_attempted = True
        if WhisperModel is None:
            self._error = "faster-whisper not installed"
            self._available = False
            return False
        device, compute_type = self._resolve_device()
        try:
            self._model = WhisperModel(
                self._config.model_size,
                device=device,
                compute_type=compute_type,
                cpu_threads=4 if device == "cpu" else 0,
            )
            self._device = device
            self._compute_type = compute_type
            LOGGER.info(
                "Transcription model ready (model=%s device=%s compute_type=%s)",
                self._config.model_size,
                device,
                compute_type,
            )
            return True
        except Exception as exc:
            LOGGER.warning("Transcription model load failed (%s): %s", device, exc)
            if device != "cpu":
                try:
                    self._model = WhisperModel(
                        self._config.model_size,
                        device="cpu",
                        compute_type="int8_float16",
                        cpu_threads=4,
                    )
                    self._device = "cpu"
                    self._compute_type = "int8_float16"
                    LOGGER.info("Transcription model loaded on CPU after GPU failure")
                    return True
                except Exception as cpu_exc:
                    self._error = str(cpu_exc)
                    LOGGER.error("Transcription unavailable: %s", cpu_exc)
                    self._available = False
                    return False
            self._error = str(exc)
            self._available = False
            return False

    def transcribe(
        self,
        media_path: Path,
        *,
        duration: Optional[float] = None,
    ) -> Optional[Tuple[str, Optional[str]]]:
        if not self._available:
            return None
        if self._config.max_duration and duration and duration > self._config.max_duration:
            LOGGER.debug(
                "Transcription skipped (duration %.1fs > limit %.1fs)",
                duration,
                self._config.max_duration,
            )
            return None
        if self._model is None and not self._load_model():
            return None
        assert self._model is not None  # for type-checkers
        options = {
            "beam_size": max(1, int(self._config.beam_size)),
            "vad_filter": bool(self._config.vad_filter),
        }
        try:
            segments, info = self._model.transcribe(str(media_path), **options)
        except Exception as exc:
            LOGGER.warning("Transcription failed: %s", exc)
            if self._device != "cpu":
                LOGGER.info("Retrying transcription on CPU")
                self._model = None
                self._load_attempted = False
                self._available = True
                self._force_cpu = True
                if self._load_model():
                    return self.transcribe(media_path, duration=duration)
            self._error = str(exc)
            return None
        transcript_parts: List[str] = []
        language = getattr(info, "language", None)
        for segment in segments:
            text = getattr(segment, "text", "")
            if not text:
                continue
            transcript_parts.append(str(text).strip())
        transcript = " ".join(part for part in transcript_parts if part)
        transcript = " ".join(transcript.split())
        if not transcript:
            return None
        return transcript, language


__all__ = ["TranscriptionConfig", "TranscriptionService"]
