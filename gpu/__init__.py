"""GPU capability detection and runtime helpers for VideoCatalog."""

from .capabilities import probe_ffmpeg_hwaccel, probe_gpu, probe_nvml, probe_onnx_providers
from .runtime import (
    ProviderName,
    get_hwaccel_args,
    get_video_hwaccel,
    last_probe_details,
    last_provider,
    providers_for_session,
    report_provider_failure,
    select_onnx_provider,
    select_provider,
)

__all__ = [
    "probe_ffmpeg_hwaccel",
    "probe_gpu",
    "probe_nvml",
    "probe_onnx_providers",
    "ProviderName",
    "get_hwaccel_args",
    "get_video_hwaccel",
    "last_probe_details",
    "last_provider",
    "providers_for_session",
    "report_provider_failure",
    "select_onnx_provider",
    "select_provider",
]
