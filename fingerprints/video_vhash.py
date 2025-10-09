from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tools import have_videohash


@dataclass(slots=True)
class VideoVHash:
    path: Path
    hash64: str


class VideoVHashError(RuntimeError):
    pass


def compute_vhash(source: Path) -> Optional[VideoVHash]:
    if not have_videohash():
        return None
    try:
        from videohash import VideoHash  # type: ignore
    except Exception as exc:  # pragma: no cover - dynamic import guard
        raise VideoVHashError(str(exc)) from exc

    try:
        vh = VideoHash(str(source), hash_size=16)
    except Exception as exc:  # pragma: no cover - underlying library errors
        raise VideoVHashError(str(exc)) from exc
    digest = getattr(vh, "videohash", None)
    if not digest:
        return None
    return VideoVHash(path=source, hash64=str(digest))


def hamming_distance(hash_a: str, hash_b: str) -> int:
    bytes_a = _to_bytes(hash_a)
    bytes_b = _to_bytes(hash_b)
    if len(bytes_a) != len(bytes_b):
        raise ValueError("Hashes must have identical length for Hamming distance")
    distance = 0
    for ba, bb in zip(bytes_a, bytes_b):
        distance += bin(ba ^ bb).count("1")
    return distance


def normalized_similarity(hash_a: str, hash_b: str) -> float:
    bytes_a = _to_bytes(hash_a)
    bytes_b = _to_bytes(hash_b)
    distance = hamming_distance(hash_a, hash_b)
    max_bits = len(bytes_a) * 8
    if max_bits == 0:
        return 0.0
    return max(0.0, 1.0 - (distance / max_bits))


def _to_bytes(value: str) -> bytes:
    cleaned = value.strip()
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return cleaned.encode("utf-8")
