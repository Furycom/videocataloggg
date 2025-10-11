"""Scoring heuristics for video quality assessment."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional

from .ffprobe import AudioStream, ProbeData, SubtitleStream, VideoStream


@dataclass(slots=True)
class QualityThresholds:
    low_bitrate_per_mp: float = 1500.0
    audio_min_channels: int = 2
    expect_subs: bool = False
    runtime_tolerance_pct: float = 10.0

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object] | None) -> "QualityThresholds":
        data = dict(mapping or {})
        return cls(
            low_bitrate_per_mp=float(data.get("low_bitrate_per_mp", 1500) or 1500),
            audio_min_channels=int(data.get("audio_min_channels", 2) or 2),
            expect_subs=bool(data.get("expect_subs", False)),
            runtime_tolerance_pct=float(data.get("runtime_tolerance_pct", 10) or 10),
        )


@dataclass(slots=True)
class QualityLabels:
    res_480p_maxh: int = 576
    res_720p_maxh: int = 800
    res_1080p_maxh: int = 1200
    res_2160p_minh: int = 1600

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object] | None) -> "QualityLabels":
        data = dict(mapping or {})
        return cls(
            res_480p_maxh=int(data.get("res_480p_maxh", 576) or 576),
            res_720p_maxh=int(data.get("res_720p_maxh", 800) or 800),
            res_1080p_maxh=int(data.get("res_1080p_maxh", 1200) or 1200),
            res_2160p_minh=int(data.get("res_2160p_minh", 1600) or 1600),
        )


@dataclass(slots=True)
class QualityInput:
    probe: ProbeData
    tmdb_runtime_min: Optional[int] = None


@dataclass(slots=True)
class QualityResult:
    score: int
    reasons: Dict[str, object]
    resolution_label: str


def resolution_label(video: Optional[VideoStream], labels: QualityLabels) -> str:
    if video is None or video.height is None:
        return "unknown"
    height = int(video.height)
    if height <= labels.res_480p_maxh:
        return "<=480p"
    if height <= labels.res_720p_maxh:
        return "720p"
    if height <= labels.res_1080p_maxh:
        return "1080p"
    if height >= labels.res_2160p_minh:
        return "2160p"
    return "1440p"


def _estimate_bitrate_per_mp(probe: ProbeData) -> Optional[float]:
    video = probe.video
    if video is None or video.width is None or video.height is None:
        return None
    width = max(1, int(video.width))
    height = max(1, int(video.height))
    megapixels = (width * height) / 1_000_000.0
    if megapixels <= 0:
        return None
    if video.bit_rate_kbps is not None and video.bit_rate_kbps > 0:
        bitrate = float(video.bit_rate_kbps)
    elif probe.bit_rate_kbps is not None and probe.bit_rate_kbps > 0:
        bitrate = float(probe.bit_rate_kbps)
    else:
        return None
    return bitrate / max(megapixels, 0.1)


def _max_audio_channels(streams: List[AudioStream]) -> Optional[int]:
    max_channels: Optional[int] = None
    for stream in streams:
        if stream.channels is None:
            continue
        value = int(stream.channels)
        if max_channels is None or value > max_channels:
            max_channels = value
    return max_channels


def _unique(items: List[Optional[str]]) -> List[str]:
    seen = []
    for item in items:
        if not item:
            continue
        token = str(item).strip().lower()
        if not token or token in seen:
            continue
        seen.append(token)
    return seen


def score_quality(
    payload: QualityInput,
    *,
    thresholds: QualityThresholds,
    labels: QualityLabels,
) -> QualityResult:
    score = 100
    reasons: Dict[str, object] = {}
    probe = payload.probe
    video = probe.video
    bitrate_per_mp = _estimate_bitrate_per_mp(probe)
    if bitrate_per_mp is not None and bitrate_per_mp < thresholds.low_bitrate_per_mp:
        gap_ratio = bitrate_per_mp / max(thresholds.low_bitrate_per_mp, 1.0)
        if gap_ratio <= 0.5:
            deduction = 25
        elif gap_ratio <= 0.75:
            deduction = 18
        else:
            deduction = 10
        score -= deduction
        reasons["low_bitrate_per_mp"] = round(bitrate_per_mp, 1)
    max_channels = _max_audio_channels(probe.audio_streams)
    if max_channels is not None and max_channels < thresholds.audio_min_channels:
        score -= 10
        reasons["audio_channels"] = max_channels
    elif max_channels is None:
        reasons.setdefault("audio_channels_unknown", True)
    if thresholds.expect_subs and not probe.subtitle_streams:
        score -= 10
        reasons["missing_subs"] = True
    if payload.tmdb_runtime_min and probe.duration_s:
        runtime_seconds = payload.tmdb_runtime_min * 60
        tolerance = (thresholds.runtime_tolerance_pct / 100.0) * runtime_seconds
        delta = abs(probe.duration_s - runtime_seconds)
        if delta > tolerance:
            score -= 10
            reasons["runtime_mismatch"] = int(delta)
        else:
            reasons.setdefault("runtime_match", True)

    audio_langs = _unique([stream.language for stream in probe.audio_streams])
    subs_langs = _unique([stream.language for stream in probe.subtitle_streams])
    if len(audio_langs) > 1:
        score += 3
        reasons["multi_audio_langs"] = len(audio_langs)
    if len(subs_langs) > 1:
        score += 3
        reasons["multi_sub_langs"] = len(subs_langs)

    score = max(0, min(100, score))
    res_label = resolution_label(video, labels)
    reasons.setdefault("resolution", res_label)
    return QualityResult(score=score, reasons=reasons, resolution_label=res_label)


def reasons_to_json(reasons: Dict[str, object]) -> str:
    try:
        return json.dumps(reasons, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return json.dumps({}, ensure_ascii=False)


__all__ = [
    "QualityInput",
    "QualityLabels",
    "QualityResult",
    "QualityThresholds",
    "resolution_label",
    "score_quality",
    "reasons_to_json",
]
