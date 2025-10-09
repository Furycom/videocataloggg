"""Tests for core.settings helpers."""

from __future__ import annotations

import json
from pathlib import Path

from core.settings import load_settings, merge_defaults, save_settings


def test_merge_defaults_includes_semantic_block() -> None:
    merged = merge_defaults({})

    assert "semantic" in merged
    semantic = merged["semantic"]

    assert semantic["enabled_default"] is False
    assert semantic["phase_mode"] == "post-scan"
    assert set(semantic["models"].keys()) == {
        "text_embedder",
        "vision_embedder",
        "video_embedder",
        "audio_embedder",
        "transcriber",
        "reranker",
    }
    assert semantic["index"]["provider"] == "sqlite"
    assert semantic["video"]["max_frames"] == 12
    assert semantic["transcribe"]["engine"] == "whisper"


def test_save_settings_upgrades_semantic_defaults(tmp_path: Path) -> None:
    working_dir = tmp_path
    path = working_dir / "settings.json"

    legacy = {
        "semantic": {
            "enabled_default": True,
            "models": {"text_embedder": "custom/text"},
        }
    }

    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = load_settings(working_dir)
    assert loaded["semantic"]["models"]["text_embedder"] == "custom/text"
    assert loaded["semantic"]["index"]["metric"] == "cosine"

    save_settings(legacy, working_dir)
    upgraded = json.loads(path.read_text(encoding="utf-8"))

    assert upgraded["semantic"]["enabled_default"] is True
    assert upgraded["semantic"]["index"]["normalize_vectors"] is True
    assert upgraded["semantic"]["video"]["frame_interval_s"] == 30
    assert upgraded["semantic"]["transcribe"]["model"] == "small"
