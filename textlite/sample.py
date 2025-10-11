"""Sampling helpers for TextLite."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

LOGGER = logging.getLogger("videocatalog.textlite.sample")


@dataclass(slots=True)
class SampleConfig:
    max_bytes: int = 32768
    max_lines: int = 400
    head_lines: int = 150
    mid_lines: int = 100
    tail_lines: int = 150


@dataclass(slots=True)
class SampleResult:
    text: str
    bytes_sampled: int
    lines_sampled: int
    sections: Tuple[int, int, int]


def _read_head(path: Path, limit: int) -> List[str]:
    if limit <= 0:
        return []
    lines: List[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline=None) as handle:
            for _ in range(limit):
                line = handle.readline()
                if not line:
                    break
                lines.append(line.rstrip("\r\n"))
    except OSError as exc:
        LOGGER.debug("Failed to read head from %s: %s", path, exc)
        return []
    return lines


def _read_tail(path: Path, limit: int, *, budget: int) -> List[str]:
    if limit <= 0 or budget <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, io.SEEK_END)
            size = handle.tell()
            chunk_size = min(size, max(1024, min(budget * 2, 65536)))
            handle.seek(max(0, size - chunk_size))
            data = handle.read(chunk_size)
    except OSError as exc:
        LOGGER.debug("Failed to read tail from %s: %s", path, exc)
        return []
    if not data:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-limit:]


def _read_mid(path: Path, limit: int, *, budget: int) -> List[str]:
    if limit <= 0 or budget <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, io.SEEK_END)
            size = handle.tell()
            if size == 0:
                return []
            chunk_size = min(size, max(1024, min(budget * 2, 65536)))
            mid_point = size // 2
            start = max(0, mid_point - chunk_size // 2)
            handle.seek(start)
            data = handle.read(chunk_size)
    except OSError as exc:
        LOGGER.debug("Failed to read mid from %s: %s", path, exc)
        return []
    if not data:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) <= limit:
        return lines
    step = max(1, len(lines) // limit)
    return [lines[i] for i in range(0, len(lines), step)][:limit]


def _dedupe_preserve(sequence: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in sequence:
        key = item.strip()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _append_line(parts: List[str], line: str, *, bytes_budget: int, used_bytes: int) -> Tuple[int, bool]:
    encoded = line.encode("utf-8")
    if used_bytes + len(encoded) <= bytes_budget:
        parts.append(line)
        return len(encoded), True
    remaining = bytes_budget - used_bytes
    if remaining <= 0:
        return 0, False
    truncated = encoded[:remaining]
    try:
        decoded = truncated.decode("utf-8", errors="ignore")
    except Exception:
        decoded = truncated.decode("utf-8", errors="replace")
    if decoded:
        parts.append(decoded)
        return remaining, True
    return 0, False


def _combine_segments(
    segments: Sequence[Tuple[str, List[str]]],
    config: SampleConfig,
) -> SampleResult:
    parts: List[str] = []
    used_bytes = 0
    used_lines = 0
    per_section: List[int] = []
    for index, (label, lines) in enumerate(segments):
        count_for_section = 0
        if not lines:
            per_section.append(0)
            continue
        if parts:
            sep = "…"
            delta, appended = _append_line(parts, sep, bytes_budget=config.max_bytes, used_bytes=used_bytes)
            if appended:
                used_bytes += delta
                used_lines += 1
        for line in lines:
            if used_lines >= config.max_lines or used_bytes >= config.max_bytes:
                break
            delta, appended = _append_line(parts, line.rstrip("\r\n"), bytes_budget=config.max_bytes, used_bytes=used_bytes)
            if not appended:
                break
            used_bytes += delta
            used_lines += 1
            count_for_section += 1
        per_section.append(count_for_section)
    text = "\n".join(parts)
    return SampleResult(text=text, bytes_sampled=used_bytes, lines_sampled=used_lines, sections=tuple(per_section[:3]))


def sample_text(path: Path, config: SampleConfig) -> SampleResult:
    """Read a limited snippet from the target file."""

    head = _read_head(path, config.head_lines)
    remaining_lines = max(0, config.max_lines - len(head))
    remaining_bytes = max(0, config.max_bytes - sum(len(line.encode("utf-8")) for line in head))
    mid_budget = max(0, min(remaining_bytes, config.max_bytes // 3))
    tail_budget = max(0, min(remaining_bytes, config.max_bytes - mid_budget))
    mid_limit = min(config.mid_lines, remaining_lines)
    tail_limit = min(config.tail_lines, remaining_lines)
    mid = _read_mid(path, mid_limit, budget=mid_budget)
    tail = _read_tail(path, tail_limit, budget=tail_budget)
    combined_segments: List[Tuple[str, List[str]]] = [
        ("head", head),
        ("mid", _dedupe_preserve(mid)),
        ("tail", _dedupe_preserve(tail)),
    ]
    return _combine_segments(combined_segments, config)


__all__ = ["SampleConfig", "SampleResult", "sample_text"]
