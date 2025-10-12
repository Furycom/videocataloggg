from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable

from .settings_schema import SETTINGS_VALIDATOR

from .paths import get_default_settings_paths

__all__ = [
    "DEFAULT_SETTINGS",
    "SETTINGS_VERSION",
    "load_settings",
    "merge_defaults",
    "save_settings",
    "update_settings",
]

SETTINGS_VERSION = 2


DEFAULT_SETTINGS: Dict[str, Any] = {
    "version": SETTINGS_VERSION,
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
    "quality": {
        "enable": True,
        "timeout_s": 8,
        "gentle_sleep_ms": 3,
        "max_parallel": 1,
        "thresholds": {
            "low_bitrate_per_mp": 1500,
            "audio_min_channels": 2,
            "expect_subs": False,
            "runtime_tolerance_pct": 10,
        },
        "labels": {
            "res_480p_maxh": 576,
            "res_720p_maxh": 800,
            "res_1080p_maxh": 1200,
            "res_2160p_minh": 1600,
        },
    },
    "disk_marker": {
        "enable": False,
        "filename": ".videocatalog.id",
        "write_hidden": True,
        "write_readonly": True,
        "use_hmac": True,
        "catalog_uuid": None,
        "hmac_key": None,
    },
    "delta_scan": {
        "use_ntfs_usn": True,
        "fallback_sampling": True,
    },
    "gpu": {
        "policy": "AUTO",
        "allow_hwaccel_video": True,
        "min_free_vram_mb": 512,
        "max_gpu_workers": 2,
    },
    "orchestrator": {
        "enable": True,
        "poll_ms": 500,
        "gpu": {"hard_requirement": True, "safety_margin_mb": 1024},
        "concurrency": {"heavy_ai_gpu": 1, "light_cpu": 2, "io_light": 2},
        "backoff": {"base_s": 30, "max_s": 600},
        "lease_ttl_s": 120,
        "heartbeat_s": 5,
    },
    "backup": {
        "enable": True,
        "schedule_cron": "0 3 * * *",
        "include_vectors": False,
        "include_thumbs": False,
        "quiesce_timeout_s": 120,
        "retention": {
            "keep_last": 10,
            "keep_daily": 14,
            "keep_weekly": 8,
            "max_total_gb": 50,
        },
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
    "assistant": {
        "enable": False,
        "runtime": "auto",
        "model": "qwen2.5:7b-instruct",
        "ctx": 8192,
        "temperature": 0.3,
        "tools_enabled": True,
        "tool_budget": 20,
        "rag": {
            "enable": True,
            "top_k": 8,
            "min_score": 0.25,
            "embed_model": "bge-small-en",
            "index": "faiss",
            "refresh_on_start": False,
        },
    },
    "structure": {
        "enable": True,
        "weights": {
            "canon": 0.35,
            "nfo": 0.25,
            "oshash": 0.20,
            "name_match": 0.15,
            "runtime": 0.05,
        },
        "low_threshold": 0.50,
        "high_threshold": 0.80,
        "opensubtitles": {
            "enabled": True,
            "read_kib": 64,
            "timeout_s": 15,
            "api_key": None,
        },
        "tmdb": {
            "enabled": True,
            "api_key": None,
        },
        "imdb": {
            "enabled": True,
        },
        "max_candidates": 5,
    },
    "learning": {
        "enable": True,
        "algo": "logreg",
        "calibration": "auto",
        "k_folds": 5,
        "min_labels": 200,
        "retrain_every_labels": 100,
        "class_weight": "balanced",
        "active": {"strategy": "uncertainty_diversity", "topN": 200},
    },
    "docpreview": {
        "enable": False,
        "max_pages": 6,
        "max_chars": 20000,
        "sample_strategy": "smart",
        "ocr_enable": True,
        "ocr_max_pages": 2,
        "ocr_timeout_s": 20,
        "summary_target_tokens": 120,
        "keywords_topk": 10,
        "gpu_allowed": True,
    },
    "textlite": {
        "enable": True,
        "max_bytes": 32768,
        "max_lines": 400,
        "head_lines": 150,
        "mid_lines": 100,
        "tail_lines": 150,
        "summary_tokens": 80,
        "keywords_topk": 8,
        "schema": {
            "csv_headers": True,
            "json_keys": True,
            "yaml_keys": True,
        },
        "skip_if_gt_mb": 50,
        "gentle_sleep_ms": 2,
        "gpu_allowed": True,
    },
    "textverify": {
        "enable": True,
        "targets": "low-medium",
        "subs_sample": {
            "max_lines": 400,
            "head_lines": 150,
            "mid_lines": 100,
            "tail_lines": 150,
            "max_chars": 20000,
        },
        "summary_tokens": 120,
        "keywords_topk": 12,
        "models": {
            "embed": "paraphrase-multilingual-MiniLM-L12-v2",
            "summarizer": "sshleifer/distilbart-cnn-12-6",
        },
        "weights": {
            "semantic": 0.55,
            "ner_overlap": 0.25,
            "keyword_overlap": 0.20,
        },
        "thresholds": {
            "boost_strong": 0.80,
            "boost_medium": 0.65,
            "flag_diverge": 0.40,
        },
        "max_items_per_run": 200,
        "gentle_sleep_ms": 3,
        "gpu_allowed": True,
    },
    "visualreview": {
        "enable": False,
        "frames_per_video": 9,
        "max_thumb_px": 640,
        "thumbnail_format": "JPEG",
        "thumbnail_quality": 85,
        "max_thumbnail_bytes": 600000,
        "max_contact_sheet_bytes": 2000000,
        "thumbnail_retention": 800,
        "sheet_retention": 400,
        "batch_size": 3,
        "sleep_seconds": 1.0,
        "ffmpeg_path": None,
        "prefer_pyav": False,
        "scene_threshold": 27.0,
        "min_scene_len": 24.0,
        "fallback_percentages": [
            0.05,
            0.20,
            0.35,
            0.50,
            0.65,
            0.80,
            0.95,
        ],
        "cropdetect": False,
        "cropdetect_frames": 12,
        "cropdetect_skip_seconds": 1.0,
        "cropdetect_round": 16,
        "allow_hwaccel": True,
        "hwaccel_policy": "AUTO",
        "sheet_columns": 4,
        "sheet_max_rows": None,
        "sheet_cell_px": [320, 180],
        "sheet_background": [16, 16, 16],
        "sheet_margin": 24,
        "sheet_padding": 6,
        "sheet_format": "WEBP",
        "sheet_quality": 80,
        "sheet_optimize": True,
    },
    "diagnostics": {
        "enable": True,
        "gpu_hard_requirement": True,
        "smoke_timeouts_s": {
            "movies": 10,
            "tv": 10,
            "apiguard": 10,
            "visual": 10,
            "docs": 10,
            "assistant": 10,
        },
        "sample_sizes": {
            "movies": 5,
            "tv": 5,
            "docs": 3,
        },
        "logs_keep_days": 14,
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


def _apply_migrations(settings: Dict[str, Any], working_dir: Path) -> Dict[str, Any]:
    version = settings.get("version")
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        version_int = 0
    if version_int < SETTINGS_VERSION:
        # Future migrations can be added here.
        settings["version"] = SETTINGS_VERSION
    return settings


def _log_unknown_keys(settings: Dict[str, Any], working_dir: Path) -> None:
    unknown = list(SETTINGS_VALIDATOR.unknown_keys(settings))
    if not unknown:
        return
    logs_dir = working_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.time(),
        "unknown": unknown,
    }
    target = logs_dir / "settings_unknown.json"
    try:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        return


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
    merged = _apply_migrations(merged, working_dir)
    merged.setdefault("working_dir", str(working_dir))
    _log_unknown_keys(merged, working_dir)
    return merged


def save_settings(settings: Dict[str, Any], working_dir: Path) -> None:
    merged = merge_defaults(dict(settings))
    merged = _apply_migrations(merged, working_dir)
    merged.setdefault("working_dir", str(working_dir))
    path = working_dir / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)


def update_settings(working_dir: Path, **values: Any) -> None:
    current = load_settings(working_dir)
    current.update(values)
    save_settings(current, working_dir)
