from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping


_ALLOWED_STRUCTURE: Dict[str, Any] = {
    "fingerprints": {
        "enable_video_tmk",
        "enable_audio_chroma",
        "enable_video_vhash_prefilter",
        "phase_mode",
        "max_concurrency",
        "batch_size",
        "io_gentle_ms",
        "tmk_bin_path",
        "fpcalc_path",
        "tmk_similarity_threshold",
        "chroma_match_threshold",
        "consensus_enabled",
        "consensus_video_weight",
    },
    "semantic": "*",
    "light_analysis": "*",
    "quality": "*",
    "disk_marker": "*",
    "delta_scan": "*",
    "gpu": "*",
    "api": "*",
    "assistant": "*",
    "structure": "*",
    "learning": "*",
    "docpreview": "*",
    "textlite": "*",
    "textverify": "*",
    "diagnostics": "*",
    "working_dir": None,
    "catalog_db": None,
    "version": None,
}


@dataclass(slots=True)
class SettingsValidator:
    schema: Mapping[str, Any]

    def unknown_keys(self, payload: Mapping[str, Any]) -> Iterable[str]:
        return sorted(self._iter_unknown(payload, self.schema, path=""))

    def _iter_unknown(self, payload: Mapping[str, Any], schema: Mapping[str, Any], *, path: str) -> Iterable[str]:
        for key, value in payload.items():
            if key not in schema:
                yield f"{path}{key}"
                continue
            rule = schema[key]
            if rule is None:
                continue
            if rule == "*":
                continue
            if isinstance(rule, set):
                if not isinstance(value, Mapping):
                    continue
                for sub in value.keys():
                    if sub not in rule:
                        yield f"{path}{key}.{sub}"
                continue
            if isinstance(rule, Mapping) and isinstance(value, Mapping):
                next_path = f"{path}{key}."
                yield from self._iter_unknown(value, rule, path=next_path)


SETTINGS_VALIDATOR = SettingsValidator(_ALLOWED_STRUCTURE)

__all__ = ["SETTINGS_VALIDATOR"]
