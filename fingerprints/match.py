from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .audio_chroma import ChromaprintGenerator
from .store import FingerprintStore
from .video_tmk import TMKVideoFingerprinter
from .video_vhash import normalized_similarity


@dataclass(slots=True)
class MatchOptions:
    use_video: bool = True
    use_audio: bool = False
    use_consensus: bool = False
    tmk_threshold: float = 0.75
    chroma_threshold: float = 0.15
    consensus_video_weight: float = 0.7
    use_prefilter: bool = True


class MatchProgress:
    def __init__(self, callback: Optional[Callable[[dict], None]]) -> None:
        self._callback = callback

    def emit(self, phase: str, **payload: object) -> None:
        if not self._callback:
            return
        message = {"type": "fingerprint_match", "phase": phase}
        message.update(payload)
        try:
            self._callback(message)
        except Exception:
            pass


def find_probable_duplicates(
    store: FingerprintStore,
    *,
    options: MatchOptions,
    fingerprinter: Optional[TMKVideoFingerprinter] = None,
    chromaprint: Optional[ChromaprintGenerator] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> List[Tuple[str, str, float, str]]:
    progress = MatchProgress(progress_callback)
    fingerprinter = fingerprinter or TMKVideoFingerprinter(None)
    chromaprint = chromaprint or ChromaprintGenerator(None)

    video_pairs = _collect_video_pairs(store, options.use_prefilter)
    audio_pairs = _collect_audio_pairs(store)

    matches: Dict[Tuple[str, str], Dict[str, float]] = {}
    results: List[Tuple[str, str, float, str]] = []

    if options.use_video and fingerprinter.available:
        progress.emit("video", total=len(video_pairs))
        for idx, (a, b, sig_a, sig_b) in enumerate(video_pairs, start=1):
            try:
                score = fingerprinter.similarity(sig_a, sig_b)
            except Exception:
                continue
            if score >= options.tmk_threshold:
                key = _pair_key(a, b)
                matches.setdefault(key, {})["video"] = score
                results.append((key[0], key[1], score, "video"))
                store.store_candidate(key[0], key[1], score, "video")
            if idx % 25 == 0:
                progress.emit("video", processed=idx, total=len(video_pairs))
        progress.emit("video", processed=len(video_pairs), total=len(video_pairs))

    if options.use_audio:
        progress.emit("audio", total=len(audio_pairs))
        for idx, (a, b, fp_a, fp_b) in enumerate(audio_pairs, start=1):
            distance = chroma_distance(fp_a, fp_b)
            if distance <= options.chroma_threshold:
                score = 1.0 - distance
                key = _pair_key(a, b)
                matches.setdefault(key, {})["audio"] = score
                results.append((key[0], key[1], score, "audio"))
                store.store_candidate(key[0], key[1], score, "audio")
            if idx % 100 == 0:
                progress.emit("audio", processed=idx, total=len(audio_pairs))
        progress.emit("audio", processed=len(audio_pairs), total=len(audio_pairs))

    if options.use_consensus:
        for key, parts in matches.items():
            if "video" not in parts or "audio" not in parts:
                continue
            video_score = parts.get("video", 0.0)
            audio_score = parts.get("audio", 0.0)
            consensus = (
                options.consensus_video_weight * video_score
                + (1.0 - options.consensus_video_weight) * audio_score
            )
            results.append((key[0], key[1], consensus, "consensus"))
            store.store_candidate(key[0], key[1], consensus, "consensus")
    return results


def _collect_video_pairs(
    store: FingerprintStore, use_prefilter: bool
) -> List[Tuple[str, str, bytes, bytes]]:
    signatures = list(store.iter_video_signatures())
    vhash_map = store.fetch_vhashes() if use_prefilter else {}
    pairs: List[Tuple[str, str, bytes, bytes]] = []
    for (path_a, sig_a, _), (path_b, sig_b, _) in itertools.combinations(signatures, 2):
        if use_prefilter and path_a in vhash_map and path_b in vhash_map:
            similarity = normalized_similarity(vhash_map[path_a], vhash_map[path_b])
            if similarity < 0.75:
                continue
        pairs.append((path_a, path_b, sig_a, sig_b))
    return pairs


def _collect_audio_pairs(store: FingerprintStore) -> List[Tuple[str, str, str, str]]:
    fingerprints = list(store.iter_audio_fingerprints())
    pairs: List[Tuple[str, str, str, str]] = []
    for (path_a, fp_a, _), (path_b, fp_b, _) in itertools.combinations(fingerprints, 2):
        pairs.append((path_a, path_b, fp_a, fp_b))
    return pairs


def chroma_distance(fp_a: str, fp_b: str) -> float:
    seq_a = _parse_chromaprint(fp_a)
    seq_b = _parse_chromaprint(fp_b)
    if not seq_a or not seq_b:
        return 1.0
    length = min(len(seq_a), len(seq_b))
    if length == 0:
        return 1.0
    mismatches = sum(1 for idx in range(length) if seq_a[idx] != seq_b[idx])
    return mismatches / float(length)


def _parse_chromaprint(raw: str) -> List[int]:
    values: List[int] = []
    for token in raw.replace("|", " ").replace(",", " ").split():
        try:
            values.append(int(token))
        except ValueError:
            continue
    return values


def _pair_key(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a <= b else (b, a)
