"""Detection helpers for TextLite previews."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger("videocatalog.textlite.detect")

_SUPPORTED_EXTS = {
    ".txt": "txt",
    ".text": "txt",
    ".md": "md",
    ".rst": "rst",
    ".log": "log",
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".ndjson": "ndjson",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".ini": "ini",
    ".cfg": "cfg",
}

_MIME_HINTS = {
    "text/plain": "txt",
    "text/markdown": "md",
    "text/x-markdown": "md",
    "text/x-log": "log",
    "text/csv": "csv",
    "text/tab-separated-values": "tsv",
    "application/json": "json",
    "application/x-ndjson": "ndjson",
    "application/yaml": "yaml",
    "text/yaml": "yaml",
    "application/x-yaml": "yaml",
    "text/x-ini": "ini",
    "text/ini": "ini",
    "text/x-config": "cfg",
}

_TEXTISH_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/yaml",
)


@dataclass(slots=True)
class DetectionResult:
    """Outcome of the detection stage for a candidate file."""

    path: Path
    kind: str
    size_bytes: int
    skipped: bool = False
    reason: Optional[str] = None


def _classify(ext: str, mime: Optional[str]) -> Optional[str]:
    ext = (ext or "").lower()
    if ext in _SUPPORTED_EXTS:
        return _SUPPORTED_EXTS[ext]
    mime = (mime or "").lower()
    if mime in _MIME_HINTS:
        return _MIME_HINTS[mime]
    for prefix in _TEXTISH_MIME_PREFIXES:
        if mime.startswith(prefix):
            return _SUPPORTED_EXTS.get(ext, "txt")
    return None


def _looks_binary(path: Path, *, chunk_size: int = 2048) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(chunk_size)
    except OSError as exc:
        LOGGER.debug("Binary probe failed for %s: %s", path, exc)
        return True
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    # Heuristic: proportion of non-printable bytes.
    text_chars = b"\n\r\t\f\b" + bytes(range(32, 127))
    nontext = sum(1 for byte in chunk if byte not in text_chars)
    return nontext > max(16, len(chunk) // 3)


def probe(
    path: Path,
    *,
    ext: str,
    mime: Optional[str],
    size_bytes: Optional[int],
    skip_if_gt_bytes: int,
) -> Optional[DetectionResult]:
    """Return a detection result when the file should be processed."""

    try:
        actual_size = int(size_bytes) if size_bytes is not None else path.stat().st_size
    except OSError as exc:
        LOGGER.debug("Stat failed for %s: %s", path, exc)
        return None
    if actual_size > skip_if_gt_bytes:
        return DetectionResult(path=path, kind="", size_bytes=actual_size, skipped=True, reason="too_large")
    kind = _classify(ext, mime)
    if not kind:
        return None
    if _looks_binary(path):
        return DetectionResult(path=path, kind=kind, size_bytes=actual_size, skipped=True, reason="binary")
    return DetectionResult(path=path, kind=kind, size_bytes=actual_size)


def supported_extensions() -> set[str]:
    return set(_SUPPORTED_EXTS.keys())


__all__ = ["DetectionResult", "probe", "supported_extensions"]
