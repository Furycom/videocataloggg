from pathlib import Path

from visualreview import VisualReviewSettings, load_visualreview_settings


def test_visualreview_settings_defaults_to_runner_config() -> None:
    settings = VisualReviewSettings.from_mapping({})
    assert settings.enable is False
    assert settings.frames_per_video == 9
    config = settings.to_runner_config(working_dir=Path("/tmp"))
    assert config.frame_sampler.max_frames == 9
    assert tuple(config.thumbnail_max_size) == (settings.max_thumb_px, settings.max_thumb_px)
    assert config.contact_sheet.columns == settings.sheet_columns
    assert config.store.thumbnail_retention == settings.thumbnail_retention


def test_load_visualreview_settings_overrides() -> None:
    payload = {
        "visualreview": {
            "enable": True,
            "frames_per_video": 5,
            "max_thumb_px": 320,
            "thumbnail_quality": 99,
            "sheet_columns": 5,
            "sheet_cell_px": [400, 200],
            "sheet_background": [32, 64, 96],
            "sheet_max_rows": 2,
            "fallback_percentages": [0.1, 0.9],
            "batch_size": 10,
            "sleep_seconds": 0.5,
            "max_thumbnail_bytes": 12345,
            "max_contact_sheet_bytes": 54321,
            "thumbnail_retention": 42,
            "sheet_retention": 24,
            "prefer_pyav": True,
            "allow_hwaccel": False,
            "hwaccel_policy": "CPU_ONLY",
        }
    }
    settings = load_visualreview_settings(payload)
    assert settings.enable is True
    assert settings.frames_per_video == 5
    assert settings.sheet_cell_px == (400, 200)
    assert settings.sheet_background == (32, 64, 96)
    assert settings.fallback_percentages == (0.1, 0.9)

    config = settings.to_runner_config(
        working_dir=Path("/tmp"),
        shard_labels=("alpha", "beta"),
    )
    assert config.frame_sampler.max_frames == 5
    assert config.frame_sampler.allow_hwaccel is False
    assert config.frame_sampler.hwaccel_policy == "CPU_ONLY"
    assert tuple(config.thumbnail_max_size) == (320, 320)
    assert config.store.max_thumbnail_bytes == 12345
    assert config.contact_sheet.columns == 5
    assert config.contact_sheet.max_rows == 2
    assert list(config.shard_labels or []) == ["alpha", "beta"]
