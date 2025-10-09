"""GPU capability probing utilities."""
from __future__ import annotations

import base64
import ctypes
import logging
import os
import subprocess
import sys
import threading
from typing import Dict, Optional

try:  # pragma: no cover - optional dependency guard
    import onnxruntime as ort  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    ort = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    import pynvml  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    pynvml = None  # type: ignore

_LOGGER = logging.getLogger("videocatalog.gpu")

_TINY_MODEL_BYTES = base64.b64decode(
    "CAwSEXZpZGVvY2F0YWxvZy50aW55OjoKDAoBWBIBWSIEUmVsdRIIVGlueVJlbHVaDwoBWBIKCggIARIECgIIAWIPCgFZEgoKCAgBEgQKAggBQgIQGA=="
)

_NVML_LOCK = threading.Lock()
_NVML_INITIALIZED = False
_NVML_FAILED = False

_FFMPEG_HWACCEL_CUDA: Optional[bool] = None
_ONNX_CUDA_OK: Optional[bool] = None
_ONNX_DML_OK: Optional[bool] = None


def _ensure_nvml() -> bool:
    global _NVML_INITIALIZED, _NVML_FAILED
    if pynvml is None or _NVML_FAILED:
        return False
    with _NVML_LOCK:
        if _NVML_FAILED:
            return False
        if _NVML_INITIALIZED:
            return True
        try:
            pynvml.nvmlInit()  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - optional dependency guard
            _NVML_FAILED = True
            _LOGGER.debug("NVML init failed: %s", exc)
            return False
        _NVML_INITIALIZED = True
        return True


def _nvml_value(func_name: str, *args) -> Optional[object]:
    if not _ensure_nvml():
        return None
    try:
        func = getattr(pynvml, func_name)
    except AttributeError:  # pragma: no cover - defensive guard
        return None
    try:
        return func(*args)
    except Exception:
        return None


def _cuda_runtime_available() -> bool:
    candidates = []
    if sys.platform.startswith("win"):
        candidates.extend([
            "nvcuda.dll",
            "cudart64_120.dll",
            "cudart64_110.dll",
            "cudart64_102.dll",
            "cudart64_101.dll",
        ])
    else:
        candidates.extend(["libcuda.so", "libcuda.so.1", "libcudart.so", "libcudart.so.1"])
    for name in candidates:
        try:
            ctypes.CDLL(name)  # type: ignore[arg-type]
            return True
        except OSError:
            continue
    return False


def _probe_onnx_provider(provider: str) -> bool:
    global _ONNX_CUDA_OK, _ONNX_DML_OK
    if ort is None:
        return False
    cache_attr = "_ONNX_CUDA_OK" if provider == "CUDAExecutionProvider" else "_ONNX_DML_OK"
    cached = globals().get(cache_attr)
    if cached is not None:
        return bool(cached)
    try:
        session = ort.InferenceSession(  # type: ignore[attr-defined]
            _TINY_MODEL_BYTES,
            providers=[provider],
        )
        available = provider in session.get_providers()
    except Exception as exc:  # pragma: no cover - runtime guard
        _LOGGER.debug("ONNX provider %s unavailable: %s", provider, exc)
        available = False
    globals()[cache_attr] = available
    return available


def _ffmpeg_supports_cuda() -> bool:
    global _FFMPEG_HWACCEL_CUDA
    if _FFMPEG_HWACCEL_CUDA is not None:
        return _FFMPEG_HWACCEL_CUDA
    candidates = [os.environ.get("VIDEOCATALOG_FFMPEG"), "ffmpeg"]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            completed = subprocess.run(
                [candidate, "-hwaccels"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except FileNotFoundError:
            continue
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.debug("ffmpeg -hwaccels failed for %s: %s", candidate, exc)
            continue
        output = "\n".join(filter(None, [completed.stdout, completed.stderr])).lower()
        if "cuda" in output.split():
            _FFMPEG_HWACCEL_CUDA = True
            return True
    _FFMPEG_HWACCEL_CUDA = False
    return False


def probe_gpu() -> Dict[str, object]:
    """Gather GPU-related capabilities and runtime readiness information."""

    result: Dict[str, object] = {
        "has_nvidia": False,
        "nv_name": None,
        "nv_vram_bytes": None,
        "nv_free_vram_bytes": None,
        "nv_driver_version": None,
        "cuda_available": False,
        "onnx_cuda_ok": False,
        "onnx_directml_ok": False,
        "ffmpeg_hwaccel_cuda": False,
    }

    handle = None
    if _ensure_nvml():
        count = _nvml_value("nvmlDeviceGetCount")  # type: ignore[arg-type]
        try:
            device_count = int(count) if count is not None else 0
        except Exception:
            device_count = 0
        if device_count > 0:
            result["has_nvidia"] = True
            handle = _nvml_value("nvmlDeviceGetHandleByIndex", 0)
            raw_name = _nvml_value("nvmlDeviceGetName", handle)
            if isinstance(raw_name, bytes):
                name = raw_name.decode("utf-8", "ignore")
            else:
                name = str(raw_name) if raw_name else None
            if name:
                result["nv_name"] = name
            mem_info = _nvml_value("nvmlDeviceGetMemoryInfo", handle)
            if mem_info is not None:
                total = getattr(mem_info, "total", None)
                free = getattr(mem_info, "free", None)
                try:
                    result["nv_vram_bytes"] = int(total) if total is not None else None
                except Exception:
                    result["nv_vram_bytes"] = None
                try:
                    result["nv_free_vram_bytes"] = int(free) if free is not None else None
                except Exception:
                    result["nv_free_vram_bytes"] = None
            driver = _nvml_value("nvmlSystemGetDriverVersion")
            if isinstance(driver, bytes):
                result["nv_driver_version"] = driver.decode("utf-8", "ignore")
            elif driver:
                result["nv_driver_version"] = str(driver)
    if result["has_nvidia"]:
        result["cuda_available"] = _cuda_runtime_available()
    result["onnx_cuda_ok"] = _probe_onnx_provider("CUDAExecutionProvider")
    result["onnx_directml_ok"] = _probe_onnx_provider("DmlExecutionProvider")
    result["ffmpeg_hwaccel_cuda"] = _ffmpeg_supports_cuda()
    return result
