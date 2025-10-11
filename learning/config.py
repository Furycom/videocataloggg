"""Configuration helpers for the learning subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional


@dataclass(slots=True)
class ActiveLearningConfig:
    """Configuration controlling the active learning queue."""

    strategy: str = "uncertainty_diversity"
    top_n: int = 200


@dataclass(slots=True)
class LearningSettings:
    """High level configuration for active learning and calibration."""

    enable: bool = True
    algo: str = "logreg"
    calibration: str = "auto"
    k_folds: int = 5
    min_labels: int = 200
    retrain_every_labels: int = 100
    class_weight: str = "balanced"
    active: ActiveLearningConfig = field(default_factory=ActiveLearningConfig)


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_learning_settings(config: Mapping[str, object]) -> LearningSettings:
    """Return :class:`LearningSettings` populated from *config* mapping."""

    section = config.get("learning") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        return LearningSettings()

    result = LearningSettings()
    result.enable = bool(section.get("enable", result.enable))
    algo = str(section.get("algo", result.algo)).strip()
    if algo:
        result.algo = algo
    calibration = str(section.get("calibration", result.calibration)).strip().lower()
    if calibration in {"sigmoid", "isotonic", "auto", "none"}:
        result.calibration = calibration
    result.k_folds = max(2, _coerce_int(section.get("k_folds"), result.k_folds))
    result.min_labels = max(0, _coerce_int(section.get("min_labels"), result.min_labels))
    result.retrain_every_labels = max(
        1, _coerce_int(section.get("retrain_every_labels"), result.retrain_every_labels)
    )
    class_weight = str(section.get("class_weight", result.class_weight)).strip().lower()
    if class_weight in {"balanced", "none"}:
        result.class_weight = class_weight

    active_raw = section.get("active")
    if isinstance(active_raw, Mapping):
        strategy = str(active_raw.get("strategy", result.active.strategy)).strip()
        if strategy:
            result.active.strategy = strategy
        top_n = active_raw.get("topN")
        if top_n is None:
            top_n = active_raw.get("top_n")
        result.active.top_n = max(1, _coerce_int(top_n, result.active.top_n))

    return result


__all__ = ["ActiveLearningConfig", "LearningSettings", "load_learning_settings"]

