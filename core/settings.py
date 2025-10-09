from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable

from .paths import get_default_settings_paths

__all__ = [
    "DEFAULT_SETTINGS",
    "load_settings",
    "merge_defaults",
    "save_settings",
    "update_settings",
]

DEFAULT_SETTINGS: Dict[str, Any] = {
    "fingerprints": {
        "enable_video_tmk": False,
        "enable_audio_chroma": False,
        "enable_video_vhash_prefilter": True,
        "phase_mode": "post-scan",
        "max_concurrency": 2,
        "batch_size": 50,
        "io_gentle_ms": 120,
        "tmk_bin_path": None,
        "fpcalc_path": None,
        "tmk_similarity_threshold": 0.75,
        "chroma_match_threshold": 0.15,
        "consensus_enabled": True,
        "consensus_video_weight": 0.7,
    },
    "semantic": {
        "enabled_default": False,
        "phase_mode": "post-scan",
        "models": {
            "text_embedder": None,
            "vision_embedder": None,
            "video_embedder": None,
            "audio_embedder": None,
            "transcriber": None,
            "reranker": None,
        },
        "index": {
            "provider": "sqlite",
            "path": None,
            "metric": "cosine",
            "normalize_vectors": True,
            "batch_size": 256,
        },
        "video": {
            "frame_interval_s": 30,
            "max_frames": 12,
            "thumbnail_size": 224,
            "prefer_ffmpeg": True,
        },
        "transcribe": {
            "engine": "whisper",
            "model": "small",
            "language": None,
            "temperature": 0.0,
            "word_timestamps": True,
            "batch_seconds": 30,
        },
    },
    "light_analysis": {
        "enabled_default": False,
        "model_path": None,
        "max_video_frames": 2,
        "prefer_ffmpeg": True,
    },
    "gpu": {
        "policy": "AUTO",
        "allow_hwaccel_video": True,
        "min_free_vram_mb": 512,
        "max_gpu_workers": 2,
    },
    "api": {
        "enabled_default": False,
        "host": "127.0.0.1",
        "port": 8756,
        "api_key": None,
        "cors_origins": ["http://localhost", "http://127.0.0.1"],
        "default_limit": 100,
        "max_page_size": 500,
    },
}


def merge_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    def _merge(default: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in default.items():
            if isinstance(value, dict):
                current = payload.get(key)
                if isinstance(current, dict):
                    result[key] = _merge(value, current)
                else:
                    result[key] = _merge(value, {})
            elif isinstance(value, list):
                current = payload.get(key)
                result[key] = list(current) if isinstance(current, list) else list(value)
            else:
                result[key] = payload.get(key, value)
        for key, value in payload.items():
            if key not in result:
                result[key] = value
        return result

    return _merge(DEFAULT_SETTINGS, data or {})


def load_settings(working_dir: Path) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for candidate in get_default_settings_paths(working_dir):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            continue
        except OSError:
            continue
        if isinstance(loaded, dict):
            data = loaded
            break
    merged = merge_defaults(data)
    merged.setdefault("working_dir", str(working_dir))
    return merged


def save_settings(settings: Dict[str, Any], working_dir: Path) -> None:
    merged = merge_defaults(dict(settings))
    merged.setdefault("working_dir", str(working_dir))
    path = working_dir / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)


def update_settings(working_dir: Path, **values: Any) -> None:
    current = load_settings(working_dir)
    current.update(values)
    save_settings(current, working_dir)
