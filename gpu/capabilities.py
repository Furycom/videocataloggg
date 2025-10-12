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
_NVML_FAILURE_REASON: Optional[str] = None

_NVML_CACHE: Optional[Dict[str, object]] = None
_ONNX_CACHE: Optional[Dict[str, Dict[str, object]]] = None
_FFMPEG_CACHE: Optional[Dict[str, bool]] = None


def _ensure_nvml() -> bool:
    global _NVML_INITIALIZED, _NVML_FAILED, _NVML_FAILURE_REASON
    if pynvml is None:
        _NVML_FAILURE_REASON = "pynvml module not available"
        return False
    if _NVML_FAILED:
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
            _NVML_FAILURE_REASON = str(exc) or "NVML initialisation failed"
            _LOGGER.debug("NVML init failed: %s", exc)
            return False
        _NVML_INITIALIZED = True
        _NVML_FAILURE_REASON = None
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


def _classify_cuda_error(message: Optional[str]) -> Dict[str, Optional[bool]]:
    """Return heuristic dependency hints based on CUDA provider error text."""

    hints: Dict[str, Optional[bool]] = {
        "driver": None,
        "cuda_toolkit": None,
        "cudnn": None,
        "msvc": None,
    }
    if not message:
        return hints
    text = message.lower()
    if "cudart" in text or "cuda runtime" in text or "cuda driver" in text:
        hints["cuda_toolkit"] = False
    if "cudnn" in text:
        hints["cudnn"] = False
    if "vcruntime" in text or "msvcp" in text or "ucrt" in text or "visual c++" in text:
        hints["msvc"] = False
    if "driver" in text and "nvidia" in text and "update" in text:
        hints["driver"] = False
    return hints


def probe_nvml(*, refresh: bool = False) -> Dict[str, object]:
    """Return information from NVML about the first NVIDIA GPU."""

    global _NVML_CACHE
    if not refresh and _NVML_CACHE is not None:
        return dict(_NVML_CACHE)

    result: Dict[str, object] = {
        "has_nvidia": False,
        "name": None,
        "vram_bytes": None,
        "free_vram_bytes": None,
        "driver_version": None,
        "error": None,
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
                result["name"] = name
            mem_info = _nvml_value("nvmlDeviceGetMemoryInfo", handle)
            if mem_info is not None:
                total = getattr(mem_info, "total", None)
                free = getattr(mem_info, "free", None)
                try:
                    result["vram_bytes"] = int(total) if total is not None else None
                except Exception:
                    result["vram_bytes"] = None
                try:
                    result["free_vram_bytes"] = int(free) if free is not None else None
                except Exception:
                    result["free_vram_bytes"] = None
            driver = _nvml_value("nvmlSystemGetDriverVersion")
            if isinstance(driver, bytes):
                result["driver_version"] = driver.decode("utf-8", "ignore")
            elif driver:
                result["driver_version"] = str(driver)
        else:
            result["error"] = "No NVIDIA GPU detected"
    else:
        if _NVML_FAILURE_REASON:
            result["error"] = _NVML_FAILURE_REASON

    _NVML_CACHE = dict(result)
    return result


def _probe_single_provider(provider: str) -> Dict[str, object]:
    status = {
        "provider": provider,
        "ok": False,
        "error": None,
    }
    if ort is None:
        status["error"] = "onnxruntime not installed"
        return status
    try:
        session = ort.InferenceSession(  # type: ignore[attr-defined]
            _TINY_MODEL_BYTES,
            providers=[provider],
        )
        available = provider in session.get_providers()
        status["ok"] = bool(available)
    except Exception as exc:  # pragma: no cover - runtime guard
        text = str(exc)
        status["error"] = text or repr(exc)
        status["ok"] = False
        _LOGGER.debug("ONNX provider %s unavailable: %s", provider, exc)
    return status


def probe_onnx_providers(*, refresh: bool = False) -> Dict[str, Dict[str, object]]:
    """Probe ONNX Runtime CUDA and DirectML execution providers."""

    global _ONNX_CACHE
    if not refresh and _ONNX_CACHE is not None:
        return {key: dict(value) for key, value in _ONNX_CACHE.items()}

    result: Dict[str, Dict[str, object]] = {
        "cuda": _probe_single_provider("CUDAExecutionProvider"),
        "directml": _probe_single_provider("DmlExecutionProvider"),
    }

    _ONNX_CACHE = {key: dict(value) for key, value in result.items()}
    return result


def probe_ffmpeg_hwaccel(*, refresh: bool = False) -> Dict[str, bool]:
    """Check whether FFmpeg exposes CUDA/NVDEC acceleration."""

    global _FFMPEG_CACHE
    if not refresh and _FFMPEG_CACHE is not None:
        return dict(_FFMPEG_CACHE)

    has_cuda = False
    candidates = [os.environ.get("VIDEOCATALOG_FFMPEG"), "ffmpeg"]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
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
        tokens = {token.strip() for token in output.split() if token.strip()}
        if "cuda" in tokens:
            has_cuda = True
            break

    cache = {"has_cuda": has_cuda}
    _FFMPEG_CACHE = dict(cache)
    return cache


def probe_gpu(*, refresh: bool = False) -> Dict[str, object]:
    """Gather GPU-related capabilities and runtime readiness information."""

    nvml_info = probe_nvml(refresh=refresh)
    provider_info = probe_onnx_providers(refresh=refresh)
    ffmpeg_info = probe_ffmpeg_hwaccel(refresh=refresh)

    cuda_ok = bool(provider_info["cuda"].get("ok"))
    cuda_error = provider_info["cuda"].get("error")
    dependency_hints = _classify_cuda_error(cuda_error)
    if nvml_info.get("has_nvidia"):
        dependency_hints.setdefault("driver", True)
    if cuda_ok:
        for key in ("cuda_toolkit", "cudnn", "msvc"):
            dependency_hints[key] = True
    elif dependency_hints.get("cuda_toolkit") is None and cuda_error:
        lowered = str(cuda_error).lower()
        if "onnxruntime" not in lowered:
            dependency_hints["cuda_toolkit"] = False

    result: Dict[str, object] = {
        "has_nvidia": bool(nvml_info.get("has_nvidia")),
        "nv_name": nvml_info.get("name"),
        "nv_vram_bytes": nvml_info.get("vram_bytes"),
        "nv_free_vram_bytes": nvml_info.get("free_vram_bytes"),
        "nv_driver_version": nvml_info.get("driver_version"),
        "nv_error": nvml_info.get("error"),
        "cuda_available": _cuda_runtime_available() if nvml_info.get("has_nvidia") else False,
        "onnx_cuda_ok": bool(provider_info["cuda"].get("ok")),
        "onnx_cuda_error": provider_info["cuda"].get("error"),
        "onnx_directml_ok": bool(provider_info["directml"].get("ok")),
        "onnx_directml_error": provider_info["directml"].get("error"),
        "ffmpeg_hwaccel_cuda": bool(ffmpeg_info.get("has_cuda")),
        "cuda_dependency_hints": dependency_hints,
    }

    return result
