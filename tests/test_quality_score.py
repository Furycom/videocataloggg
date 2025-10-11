import importlib.util
import math
import sys
import types
from pathlib import Path
from types import SimpleNamespace

_quality_path = Path(__file__).resolve().parents[1] / "quality"
_quality_pkg = types.ModuleType("quality")
_quality_pkg.__path__ = [str(_quality_path)]
sys.modules.setdefault("quality", _quality_pkg)

_SCORE_SPEC = importlib.util.spec_from_file_location(
    "quality.score", _quality_path / "score.py", submodule_search_locations=_quality_pkg.__path__
)
if _SCORE_SPEC is None or _SCORE_SPEC.loader is None:  # pragma: no cover - defensive
    raise RuntimeError("Unable to load quality.score module for tests")
_score_module = importlib.util.module_from_spec(_SCORE_SPEC)
sys.modules["quality.score"] = _score_module
_SCORE_SPEC.loader.exec_module(_score_module)

QualityInput = _score_module.QualityInput
QualityLabels = _score_module.QualityLabels
QualityThresholds = _score_module.QualityThresholds
resolution_label = _score_module.resolution_label
score_quality = _score_module.score_quality


def _make_video(
    *, codec: str, width: int, height: int, bit_rate_kbps: int | None, avg_frame_rate: str
) -> SimpleNamespace:
    return SimpleNamespace(
        codec=codec,
        width=width,
        height=height,
        bit_rate_kbps=bit_rate_kbps,
        avg_frame_rate=avg_frame_rate,
    )


def _make_audio(*, codec: str, channels: int | None, language: str | None) -> SimpleNamespace:
    return SimpleNamespace(codec=codec, channels=channels, language=language)


def _make_subtitle(*, codec: str, language: str | None) -> SimpleNamespace:
    return SimpleNamespace(codec=codec, language=language)


def _make_probe(
    *,
    video: SimpleNamespace,
    audio_streams: list[SimpleNamespace] | None = None,
    subtitle_streams: list[SimpleNamespace] | None = None,
    duration_s: float = 5400.0,
    bit_rate_kbps: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        container="mkv",
        duration_s=duration_s,
        bit_rate_kbps=bit_rate_kbps,
        video=video,
        audio_streams=audio_streams or [],
        subtitle_streams=subtitle_streams or [],
    )


def test_score_quality_penalizes_low_bitrate_density() -> None:
    video = _make_video(
        codec="h264",
        width=1920,
        height=1080,
        bit_rate_kbps=2000,
        avg_frame_rate="24/1",
    )
    probe = _make_probe(
        video=video,
        audio_streams=[_make_audio(codec="aac", channels=2, language="eng")],
    )
    thresholds = QualityThresholds(low_bitrate_per_mp=1500.0, audio_min_channels=2)
    labels = QualityLabels()
    result = score_quality(QualityInput(probe=probe), thresholds=thresholds, labels=labels)
    assert result.score == 82
    assert "low_bitrate_per_mp" in result.reasons
    assert result.reasons["resolution"] == "1080p"


def test_score_quality_applies_bonuses_for_multiple_languages() -> None:
    video = _make_video(
        codec="hevc",
        width=3840,
        height=2160,
        bit_rate_kbps=24000,
        avg_frame_rate="24000/1001",
    )
    audio_streams = [
        _make_audio(codec="eac3", channels=6, language="eng"),
        _make_audio(codec="aac", channels=2, language="spa"),
    ]
    subtitle_streams = [
        _make_subtitle(codec="srt", language="eng"),
        _make_subtitle(codec="srt", language="spa"),
    ]
    probe = _make_probe(
        video=video,
        audio_streams=audio_streams,
        subtitle_streams=subtitle_streams,
        duration_s=7200.0,
        bit_rate_kbps=26000,
    )
    thresholds = QualityThresholds(expect_subs=True)
    labels = QualityLabels()
    result = score_quality(QualityInput(probe=probe), thresholds=thresholds, labels=labels)
    assert result.score == 100
    assert result.reasons.get("multi_audio_langs") == 2
    assert result.reasons.get("multi_sub_langs") == 2
    assert "missing_subs" not in result.reasons
    assert resolution_label(video, labels) == "2160p"


def test_score_quality_runtime_mismatch_and_audio_penalty() -> None:
    video = _make_video(
        codec="h264",
        width=1280,
        height=720,
        bit_rate_kbps=4500,
        avg_frame_rate="24/1",
    )
    probe = _make_probe(
        video=video,
        audio_streams=[_make_audio(codec="aac", channels=1, language="eng")],
        duration_s=7200.0,
    )
    thresholds = QualityThresholds(audio_min_channels=2, runtime_tolerance_pct=10.0)
    labels = QualityLabels()
    payload = QualityInput(probe=probe, tmdb_runtime_min=100)
    result = score_quality(payload, thresholds=thresholds, labels=labels)
    assert result.score == 80
    assert result.reasons.get("audio_channels") == 1
    mismatch = result.reasons.get("runtime_mismatch")
    assert isinstance(mismatch, int)
    assert math.isclose(mismatch, 1200, rel_tol=0, abs_tol=1)
