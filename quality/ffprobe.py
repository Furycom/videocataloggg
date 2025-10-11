"""Minimal ffprobe wrapper used by the video quality pipeline."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

# Optional helpers for language normalization
try:  # pragma: no cover - optional dependency
    from langcodes import Language  # type: ignore
except Exception:  # pragma: no cover - degrade gracefully
    Language = None  # type: ignore

try:  # pragma: no cover - optional dependency
    from iso639 import languages as iso_languages  # type: ignore
except Exception:  # pragma: no cover - degrade gracefully
    iso_languages = None  # type: ignore


@dataclass(slots=True)
class VideoStream:
    codec: Optional[str]
    width: Optional[int]
    height: Optional[int]
    bit_rate_kbps: Optional[int]
    avg_frame_rate: Optional[str]


@dataclass(slots=True)
class AudioStream:
    codec: Optional[str]
    channels: Optional[int]
    language: Optional[str]


@dataclass(slots=True)
class SubtitleStream:
    codec: Optional[str]
    language: Optional[str]


@dataclass(slots=True)
class ProbeData:
    container: Optional[str]
    duration_s: Optional[float]
    bit_rate_kbps: Optional[int]
    video: Optional[VideoStream]
    audio_streams: List[AudioStream]
    subtitle_streams: List[SubtitleStream]


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    data: Optional[ProbeData] = None
    error: Optional[str] = None
    reason: Optional[str] = None


def ffprobe_available() -> bool:
    """Return True when ffprobe is available on PATH."""

    return shutil.which("ffprobe") is not None


def _normalize_language(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text.replace("_", "-")
    # Try langcodes for smart mapping to ISO 639-1 when possible.
    if Language is not None:  # pragma: no cover - depends on optional package
        try:
            lang = Language.get(candidate)
            if lang.language:
                return lang.language.lower()
            normalized = lang.to_tag()
            if normalized:
                return normalized.lower()
        except Exception:
            pass
    lowered = candidate.lower()
    if len(lowered) == 2:
        return lowered
    if len(lowered) == 3:
        if iso_languages is not None:  # pragma: no cover - optional dependency
            try:
                entry = iso_languages.get(part3=lowered)
                if entry and getattr(entry, "alpha2", None):
                    return entry.alpha2.lower()
            except Exception:
                pass
        return lowered
    if "-" in lowered:
        return lowered.split("-", 1)[0]
    if lowered:
        return lowered[:3]
    return None


def _safe_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def run_ffprobe(path: str, *, timeout: float) -> ProbeResult:
    """Execute ffprobe for *path* and return structured stream metadata."""

    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return ProbeResult(ok=False, error="ffprobe not found", reason="missing_tool")

    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        path,
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(1.0, float(timeout)),
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(ok=False, error="ffprobe timeout", reason="probe_timeout")
    except FileNotFoundError:
        return ProbeResult(ok=False, error="ffprobe not found", reason="missing_tool")
    except Exception as exc:
        return ProbeResult(ok=False, error=f"ffprobe failed: {exc}", reason="probe_error")

    if proc.returncode != 0:
        error_msg = proc.stderr.strip() or proc.stdout.strip() or "ffprobe error"
        reason = "probe_error"
        if "Permission denied" in error_msg or "Input/output error" in error_msg:
            reason = "probe_error"
        return ProbeResult(ok=False, error=error_msg, reason=reason)

    try:
        parsed = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return ProbeResult(ok=False, error=f"invalid ffprobe output: {exc}", reason="probe_error")

    format_section = parsed.get("format") if isinstance(parsed.get("format"), dict) else {}
    container = format_section.get("format_name") or format_section.get("format_long_name")
    duration = _safe_float(format_section.get("duration"))
    bit_rate = _safe_int(format_section.get("bit_rate"))
    if bit_rate is not None:
        bit_rate //= 1000

    video_stream: Optional[VideoStream] = None
    audio_streams: List[AudioStream] = []
    subtitle_streams: List[SubtitleStream] = []

    streams = parsed.get("streams") if isinstance(parsed.get("streams"), list) else []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "").lower()
        codec_name = stream.get("codec_name") or stream.get("codec_long_name")
        if codec_type == "video":
            if video_stream is None:
                video_stream = VideoStream(
                    codec=str(codec_name) if codec_name else None,
                    width=_safe_int(stream.get("width")),
                    height=_safe_int(stream.get("height")),
                    bit_rate_kbps=_safe_int(stream.get("bit_rate")) if stream.get("bit_rate") else None,
                    avg_frame_rate=str(stream.get("avg_frame_rate")) if stream.get("avg_frame_rate") else None,
                )
        elif codec_type == "audio":
            tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
            lang = (
                tags.get("language")
                or tags.get("LANGUAGE")
                or tags.get("lang")
                or stream.get("tags:language")
            )
            audio_streams.append(
                AudioStream(
                    codec=str(codec_name) if codec_name else None,
                    channels=_safe_int(stream.get("channels")),
                    language=_normalize_language(lang),
                )
            )
        elif codec_type == "subtitle":
            tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
            lang = tags.get("language") or tags.get("LANGUAGE") or tags.get("lang")
            subtitle_streams.append(
                SubtitleStream(
                    codec=str(codec_name) if codec_name else None,
                    language=_normalize_language(lang),
                )
            )

    data = ProbeData(
        container=str(container) if container else None,
        duration_s=duration,
        bit_rate_kbps=bit_rate,
        video=video_stream,
        audio_streams=audio_streams,
        subtitle_streams=subtitle_streams,
    )
    return ProbeResult(ok=True, data=data)


__all__ = [
    "ProbeData",
    "ProbeResult",
    "VideoStream",
    "AudioStream",
    "SubtitleStream",
    "ffprobe_available",
    "run_ffprobe",
]
