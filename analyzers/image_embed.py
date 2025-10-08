"""ONNX-based image embedding utilities for lightweight analysis."""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageOps

try:  # pragma: no cover - optional dependency guard
    import onnxruntime as ort  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    ort = None  # type: ignore


class LightAnalysisModelError(RuntimeError):
    """Raised when the MobileNet ONNX model or runtime cannot be loaded."""


@dataclass(frozen=True)
class ImageEmbedderConfig:
    model_path: Path
    input_size: int = 224


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
        try:
            self._session = ort.InferenceSession(  # type: ignore[attr-defined]
                str(model_path), providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # pragma: no cover - runtime guard
            raise LightAnalysisModelError(f"Failed to initialize ONNXRuntime: {exc}") from exc
        self._input_name = self._session.get_inputs()[0].name
        height = self._infer_size(self._session.get_inputs()[0].shape)
        self._size = int(height or config.input_size)

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
        except Exception:
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

    def _prepare(self, image: Image.Image) -> np.ndarray:
        img = ImageOps.exif_transpose(image).convert("RGB")
        if self._size:
            img = img.resize((self._size, self._size), Image.BILINEAR)
        arr = np.asarray(img, dtype=np.float32) / 255.0
        arr = (arr - _MEAN) / _STD
        arr = np.transpose(arr, (2, 0, 1))
        arr = np.expand_dims(arr, axis=0)
        return arr.astype(np.float32, copy=False)
