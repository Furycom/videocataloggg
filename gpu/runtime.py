"""GPU runtime selection helpers."""
from __future__ import annotations

import logging
from typing import Dict, List, Literal, Optional

from .capabilities import probe_gpu

ProviderName = Literal["CUDAExecutionProvider", "DmlExecutionProvider", "CPU"]

_LOGGER = logging.getLogger("videocatalog.gpu")

_GPU_DISABLED = False
_GPU_FAILURE_REPORTED = False
_LOGGED_NO_GPU = False
_LAST_CAPS: Dict[str, object] = {}
_LAST_PROVIDER: ProviderName = "CPU"


def report_provider_failure(provider: ProviderName, error: Exception) -> None:
    """Disable GPU usage after a provider failure and log it once."""

    global _GPU_DISABLED, _GPU_FAILURE_REPORTED
    if provider == "CPU":
        return
    if not _GPU_FAILURE_REPORTED:
        _LOGGER.warning(
            "GPU: %s provider failed (%s) — falling back to CPU", provider, error
        )
        _GPU_FAILURE_REPORTED = True
    _GPU_DISABLED = True


def _enforce_vram_threshold(caps: Dict[str, object], min_free_vram_mb: int) -> bool:
    if min_free_vram_mb <= 0:
        return True
    free_bytes = caps.get("nv_free_vram_bytes")
    if free_bytes is None:
        return True
    try:
        free_mb = int(free_bytes) / (1024 * 1024)
    except Exception:
        return True
    if free_mb < min_free_vram_mb:
        _LOGGER.warning(
            "GPU: free VRAM %.0f MB below threshold (%d MB) — using CPU",
            free_mb,
            min_free_vram_mb,
        )
        return False
    return True


def _log_no_gpu_once() -> None:
    global _LOGGED_NO_GPU
    if not _LOGGED_NO_GPU:
        _LOGGER.info("GPU: NVIDIA not detected — running CPU")
        _LOGGED_NO_GPU = True


def _log_cuda_error(caps: Dict[str, object]) -> None:
    error = caps.get("onnx_cuda_error")
    if not error:
        return
    _LOGGER.debug("GPU: CUDA provider error: %s", error)


def select_provider(
    policy: str = "AUTO",
    *,
    min_free_vram_mb: int = 0,
    caps: Optional[Dict[str, object]] = None,
) -> ProviderName:
    """Pick the best ONNX Runtime provider based on policy and capabilities."""

    global _GPU_DISABLED, _LAST_CAPS, _LAST_PROVIDER
    normalized = (policy or "AUTO").upper()
    capabilities = probe_gpu() if caps is None else caps
    _LAST_CAPS = dict(capabilities)

    if normalized == "CPU_ONLY" or _GPU_DISABLED:
        if not capabilities.get("has_nvidia"):
            _log_no_gpu_once()
        _LAST_PROVIDER = "CPU"
        return "CPU"

    provider: ProviderName = "CPU"

    has_cuda = bool(capabilities.get("onnx_cuda_ok"))
    has_dml = bool(capabilities.get("onnx_directml_ok"))

    if has_cuda and _enforce_vram_threshold(capabilities, min_free_vram_mb):
        provider = "CUDAExecutionProvider"
    elif has_dml:
        if not has_cuda:
            _LOGGER.info("GPU: CUDA EP unavailable — falling back to DirectML")
        provider = "DmlExecutionProvider"
    else:
        provider = "CPU"

    if provider == "CPU":
        if normalized == "FORCE_GPU":
            _LOGGER.warning(
                "GPU: FORCE_GPU requested but no compatible provider found — using CPU"
            )
        if not capabilities.get("has_nvidia"):
            _log_no_gpu_once()
        _LAST_PROVIDER = provider
        return provider

    if provider == "CUDAExecutionProvider" and not capabilities.get("cuda_available"):
        _LOGGER.info("GPU: CUDA runtime missing — using CPU instead")
        provider = "CPU"
    if provider == "CPU" and has_dml:
        _LOGGER.info("GPU: falling back to DirectML due to CUDA runtime issues")
        provider = "DmlExecutionProvider"

    if not has_cuda:
        _log_cuda_error(capabilities)

    if provider == "DmlExecutionProvider" and not has_dml:
        provider = "CPU"

    _LAST_PROVIDER = provider
    return provider


def select_onnx_provider(
    policy: str = "AUTO",
    *,
    min_free_vram_mb: int = 0,
    caps: Optional[Dict[str, object]] = None,
) -> ProviderName:
    """Backward-compatible alias for :func:`select_provider`."""

    return select_provider(policy, min_free_vram_mb=min_free_vram_mb, caps=caps)


def providers_for_session(provider: ProviderName) -> List[str]:
    if provider == "CUDAExecutionProvider":
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if provider == "DmlExecutionProvider":
        return ["DmlExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def get_hwaccel_args(
    policy: str = "AUTO",
    *,
    allow_hwaccel: bool = True,
    caps: Optional[Dict[str, object]] = None,
) -> List[str]:
    """Return FFmpeg hwaccel arguments when CUDA decoding is available."""

    normalized = (policy or "AUTO").upper()
    if not allow_hwaccel or normalized == "CPU_ONLY" or _GPU_DISABLED:
        return []
    capabilities = probe_gpu() if caps is None else caps
    if not capabilities.get("has_nvidia"):
        return []
    if not capabilities.get("ffmpeg_hwaccel_cuda"):
        return []
    return ["-hwaccel", "cuda"]


def get_video_hwaccel(
    policy: str = "AUTO",
    *,
    allow_hwaccel: bool = True,
    caps: Optional[Dict[str, object]] = None,
) -> List[str]:
    """Backward-compatible alias for :func:`get_hwaccel_args`."""

    return get_hwaccel_args(policy, allow_hwaccel=allow_hwaccel, caps=caps)


def last_probe_details() -> Dict[str, object]:
    """Return the most recent capabilities snapshot used for selection."""

    return dict(_LAST_CAPS)


def last_provider() -> ProviderName:
    """Return the most recently selected provider."""

    return _LAST_PROVIDER
