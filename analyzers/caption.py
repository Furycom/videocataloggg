"""Optional BLIP captioning helpers."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

from gpu.capabilities import probe_gpu

LOGGER = logging.getLogger("videocatalog.caption")

try:  # pragma: no cover - optional dependency guard
    import torch
except Exception:  # pragma: no cover - optional dependency guard
    torch = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    from transformers import BlipForConditionalGeneration, BlipProcessor  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    BlipForConditionalGeneration = None  # type: ignore
    BlipProcessor = None  # type: ignore


@dataclass(frozen=True)
class CaptionConfig:
    enabled: bool = False
    model_name: str = "Salesforce/blip-image-captioning-base"
    device_policy: str = "AUTO"
    max_length: int = 64


class CaptionService:
    """Generate captions for images using BLIP."""

    def __init__(self, config: CaptionConfig) -> None:
        self._config = config
        self._device = "cpu"
        self._model = None
        self._processor = None
        self._available = bool(config.enabled)
        self._load_attempted = False
        self._force_cpu = False
        if not self._available:
            return
        if torch is None or BlipForConditionalGeneration is None or BlipProcessor is None:
            LOGGER.info("Captioning disabled — transformers or torch missing")
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def _resolve_device(self) -> str:
        if self._force_cpu:
            return "cpu"
        policy = (self._config.device_policy or "AUTO").upper()
        if torch is None:
            return "cpu"
        caps = probe_gpu()
        if policy != "CPU_ONLY" and caps.get("has_nvidia") and torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():  # type: ignore[attr-defined]
            return "mps"
        return "cpu"

    def _load_model(self) -> bool:
        if not self._available or self._load_attempted:
            return self._model is not None and self._processor is not None
        self._load_attempted = True
        if torch is None or BlipForConditionalGeneration is None or BlipProcessor is None:
            self._available = False
            return False
        device = self._resolve_device()
        try:
            self._processor = BlipProcessor.from_pretrained(self._config.model_name)
            self._model = BlipForConditionalGeneration.from_pretrained(self._config.model_name)
            self._model.to(device)
            self._device = device
            LOGGER.info("Caption model ready on %s", device)
            return True
        except Exception as exc:
            LOGGER.warning("Caption model load failed (%s): %s", device, exc)
            if device != "cpu":
                self._force_cpu = True
                self._load_attempted = False
                return self._load_model()
            self._available = False
            return False

    def generate(self, image_path: Path) -> Optional[str]:
        if not self._available:
            return None
        if self._model is None or self._processor is None:
            if not self._load_model():
                return None
        assert self._model is not None and self._processor is not None
        try:
            with Image.open(image_path) as image:
                return self._caption_image(image)
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _caption_image(self, image: Image.Image) -> Optional[str]:
        if torch is None or self._model is None or self._processor is None:
            return None
        try:
            inputs = self._processor(image, return_tensors="pt").to(self._device)
            output = self._model.generate(
                **inputs,
                max_length=max(16, int(self._config.max_length)),
                num_beams=3,
            )
            caption = self._processor.decode(output[0], skip_special_tokens=True)
        except Exception as exc:
            LOGGER.debug("Caption generation failed: %s", exc)
            if self._device != "cpu":
                self._force_cpu = True
                self._model = None
                self._processor = None
                self._load_attempted = False
                if self._load_model():
                    return self._caption_image(image)
            return None
        return caption.strip()


__all__ = ["CaptionConfig", "CaptionService"]
