"""ONNX-based image embedding utilities for lightweight analysis."""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
from PIL import Image, ImageOps

try:  # pragma: no cover - optional dependency guard
    import onnxruntime as ort  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    ort = None  # type: ignore

from gpu.runtime import report_provider_failure


class LightAnalysisModelError(RuntimeError):
    """Raised when the MobileNet ONNX model or runtime cannot be loaded."""


@dataclass(frozen=True)
class ImageEmbedderConfig:
    model_path: Path
    input_size: int = 224
    providers: Optional[Sequence[str]] = None
    primary_provider: str = "CPUExecutionProvider"


_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class ImageEmbedder:
    """Thin wrapper around ONNXRuntime to embed RGB images."""

    def __init__(self, config: ImageEmbedderConfig) -> None:
        if ort is None:
            raise LightAnalysisModelError(
                "onnxruntime is not available. Install onnxruntime to enable light analysis."
            )
        model_path = config.model_path.expanduser().resolve()
        if not model_path.exists():
            raise LightAnalysisModelError(
                f"ONNX model not found at {model_path}. Place a MobileNetV3-Small model there."
            )
        providers = list(config.providers) if config.providers else ["CPUExecutionProvider"]
        if not providers:
            providers = ["CPUExecutionProvider"]
        primary_provider = config.primary_provider or providers[0]
        self._model_path = model_path
        self._config_input_size = int(config.input_size)
        self._providers: list[str] = []
        self._primary_provider: str = primary_provider
        self._fallback_triggered = False
        self._initialize_session(providers, primary_provider)

    @staticmethod
    def _infer_size(shape: list[Optional[int]]) -> int:
        try:
            if len(shape) >= 4 and shape[2] and shape[3]:
                return int(shape[2])
        except Exception:
            return 0
        return 0

    @property
    def input_size(self) -> int:
        return self._size

    def embed_path(self, path: Path) -> Optional[np.ndarray]:
        try:
            with Image.open(path) as img:
                return self.embed_image(img)
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def embed_bytes(self, data: bytes) -> Optional[np.ndarray]:
        try:
            with Image.open(io.BytesIO(data)) as img:
                return self.embed_image(img)
        except OSError:
            return None

    def embed_image(self, image: Image.Image) -> Optional[np.ndarray]:
        try:
            prepared = self._prepare(image)
            outputs = self._session.run(None, {self._input_name: prepared})
        except Exception as exc:
            if (
                self._primary_provider != "CPUExecutionProvider"
                and not self._fallback_triggered
            ):
                self._fallback_triggered = True
                try:
                    report_provider_failure(self._primary_provider, exc)
                except Exception:
                    pass
                try:
                    self._switch_to_cpu()
                except LightAnalysisModelError:
                    return None
                return self.embed_image(image)
            return None
        if not outputs:
            return None
        vector = outputs[0]
        if isinstance(vector, list):  # pragma: no cover - defensive
            vector = np.asarray(vector, dtype=np.float32)
        if not isinstance(vector, np.ndarray):
            vector = np.asarray(vector, dtype=np.float32)
        if vector.ndim == 2 and vector.shape[0] == 1:
            vector = vector[0]
        vector = vector.astype(np.float32, copy=False)
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        return vector

    def _initialize_session(self, providers: Sequence[str], primary: Optional[str] = None) -> None:
        try:
            session = ort.InferenceSession(  # type: ignore[attr-defined]
                str(self._model_path), providers=list(providers)
            )
        except Exception as exc:  # pragma: no cover - runtime guard
            providers_desc = ", ".join(providers) if providers else "CPUExecutionProvider"
            raise LightAnalysisModelError(
                f"Failed to initialize ONNXRuntime ({providers_desc}): {exc}"
            ) from exc
        self._session = session
        self._providers = list(providers)
        if primary:
            self._primary_provider = primary
        else:
            self._primary_provider = (
                self._providers[0] if self._providers else "CPUExecutionProvider"
            )
        inputs = self._session.get_inputs()
        self._input_name = inputs[0].name
        height = self._infer_size(inputs[0].shape)
        self._size = int(height or self._config_input_size)

    def _switch_to_cpu(self) -> None:
        self._initialize_session(["CPUExecutionProvider"], "CPUExecutionProvider")

    def _prepare(self, image: Image.Image) -> np.ndarray:
        img = ImageOps.exif_transpose(image).convert("RGB")
        if self._size:
            img = img.resize((self._size, self._size), Image.BILINEAR)
        arr = np.asarray(img, dtype=np.float32) / 255.0
        arr = (arr - _MEAN) / _STD
        arr = np.transpose(arr, (2, 0, 1))
        arr = np.expand_dims(arr, axis=0)
        return arr.astype(np.float32, copy=False)
