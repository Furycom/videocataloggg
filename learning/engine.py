"""High level orchestration for active learning and calibration."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .config import LearningSettings
from .db import (
    LearningExample,
    count_examples,
    ensure_learning_tables,
    insert_example,
    latest_model_record,
    load_examples,
    load_runtime_config,
    record_model,
    upsert_runtime_config,
)
from .features import FeatureExtractor
from .model import TrainingArtifacts, load_pipeline, reliability_as_dict, score_items, train_model

LOGGER = logging.getLogger("videocatalog.learning")


@dataclass(slots=True)
class ActiveItem:
    path: str
    features: Dict[str, float]
    p_correct: float
    uncertainty: float
    source_confidence: float
    item_type: str
    reasons: List[str]


@dataclass(slots=True)
class LearningExamplePayload:
    path: str
    label: int
    label_source: str
    features: Dict[str, float]


class LearningEngine:
    """Coordinates feature extraction, training, inference and queues."""

    def __init__(
        self,
        conn,
        *,
        working_dir: Path,
        settings: LearningSettings,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.working_dir = Path(working_dir)
        ensure_learning_tables(self.conn)
        upsert_runtime_config(
            self.conn,
            k=settings.k_folds,
            min_labels=settings.min_labels,
            retrain_every_labels=settings.retrain_every_labels,
            class_weight=settings.class_weight,
            active_topn=settings.active.top_n,
            active_strategy=settings.active.strategy,
        )
        self.feature_extractor = FeatureExtractor(self.conn)
        self.feature_names: List[str] = []
        self.model_version: Optional[str] = None
        self.pipeline = None
        self.metrics: Dict[str, object] = {}
        self._load_latest_model()

    # ------------------------------------------------------------------
    # Model management
    # ------------------------------------------------------------------

    def _load_latest_model(self) -> None:
        record = latest_model_record(self.conn)
        if record is None:
            self.pipeline = None
            self.feature_names = []
            self.model_version = None
            self.metrics = {}
            return
        try:
            metrics = json.loads(record["metrics_json"])
        except Exception:
            metrics = {}
        feature_names = metrics.get("feature_names") if isinstance(metrics, dict) else None
        if not isinstance(feature_names, list):
            feature_names = []
        self.feature_names = [str(name) for name in feature_names]
        self.pipeline = load_pipeline(self.working_dir, self.feature_names)
        self.model_version = record["version"]
        self.metrics = metrics if isinstance(metrics, dict) else {}

    def reload(self) -> None:
        """Reload the latest model from disk."""

        self._load_latest_model()

    # ------------------------------------------------------------------
    # Label ingestion
    # ------------------------------------------------------------------

    def record_feedback(self, payload: LearningExamplePayload) -> None:
        insert_example(
            self.conn,
            path=payload.path,
            label=int(payload.label),
            label_source=payload.label_source,
            features=payload.features,
        )

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def should_train(self) -> bool:
        total = count_examples(self.conn)
        if total < self.settings.min_labels:
            return False
        record = load_runtime_config(self.conn)
        if record is None:
            return total >= self.settings.min_labels
        last_model = latest_model_record(self.conn)
        if last_model is None:
            return total >= self.settings.min_labels
        try:
            trained_n = json.loads(last_model["metrics_json"]).get("n", 0)
        except Exception:
            trained_n = 0
        delta = total - int(trained_n or 0)
        return delta >= self.settings.retrain_every_labels

    def train(self) -> Optional[TrainingArtifacts]:
        examples = load_examples(self.conn)
        artifacts = train_model(examples, settings=self.settings, working_dir=self.working_dir)
        if artifacts is None:
            return None
        metrics = dict(artifacts.metrics)
        metrics["reliability"] = reliability_as_dict(artifacts.reliability)
        metrics["feature_names"] = artifacts.feature_names
        record_model(
            self.conn,
            version=artifacts.version,
            algo=self.settings.algo,
            calibrated=artifacts.calibrator,
            onnx_path=str(artifacts.onnx_path.relative_to(self.working_dir))
            if artifacts.onnx_path is not None
            else None,
            metrics=metrics,
        )
        try:
            self.conn.commit()
        except Exception:
            pass
        self.feature_names = artifacts.feature_names
        self.pipeline = load_pipeline(self.working_dir, self.feature_names)
        self.model_version = artifacts.version
        self.metrics = metrics
        return artifacts

    # ------------------------------------------------------------------
    # Inference helpers
    # ------------------------------------------------------------------

    def _score_features(self, features: Mapping[str, float]) -> float:
        if self.pipeline is None:
            return float(features.get("confidence_rule", 0.0))
        scores = score_items(self.pipeline, [features])
        if not scores:
            return float(features.get("confidence_rule", 0.0))
        return max(0.0, min(1.0, float(scores[0])))

    def score_path(self, path: str) -> Optional[Dict[str, object]]:
        features = self.feature_extractor.collect(path)
        if features is None:
            return None
        probability = self._score_features(features)
        version = self.model_version or "rule"
        return {
            "p_correct": probability,
            "version": version,
            "features_used": sorted(features.keys()),
        }

    # ------------------------------------------------------------------
    # Active learning queue
    # ------------------------------------------------------------------

    def _load_candidate_paths(self) -> List[Tuple[str, float, str]]:
        labeled = {row[0] for row in self.conn.execute("SELECT path FROM learn_examples")}
        candidates: List[Tuple[str, float, str]] = []
        cursor = self.conn.execute(
            "SELECT folder_path, confidence FROM review_queue ORDER BY confidence ASC LIMIT ?",
            (self.settings.active.top_n * 2,),
        )
        for row in cursor.fetchall():
            path = row[0]
            if path in labeled:
                continue
            try:
                conf = float(row[1] or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            candidates.append((path, conf, "movie"))
        cursor = self.conn.execute(
            """
            SELECT item_key, confidence
            FROM tv_review_queue
            WHERE item_type = 'episode'
            ORDER BY confidence ASC
            LIMIT ?
            """,
            (self.settings.active.top_n * 2,),
        )
        for row in cursor.fetchall():
            path = row[0]
            if path in labeled:
                continue
            try:
                conf = float(row[1] or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            candidates.append((path, conf, "episode"))
        return candidates

    def _format_reasons(self, features: Mapping[str, float]) -> List[str]:
        items = sorted(features.items(), key=lambda kv: abs(float(kv[1])), reverse=True)
        reasons: List[str] = []
        for name, value in items[:5]:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            reasons.append(f"{name}={numeric:.2f}")
        return reasons

    def _vectorize(self, features: Mapping[str, float], names: Sequence[str]) -> np.ndarray:
        return np.array([float(features.get(name, 0.0)) for name in names], dtype=float)

    def build_active_queue(self, limit: Optional[int] = None) -> List[ActiveItem]:
        pool = self._load_candidate_paths()
        if not pool:
            return []
        limit = limit or self.settings.active.top_n
        entries: List[Tuple[str, float, str, Dict[str, float], float]] = []
        for path, conf, item_type in pool:
            features = self.feature_extractor.collect(path)
            if not features:
                continue
            probability = self._score_features(features)
            uncertainty = abs(probability - 0.5)
            entries.append((path, conf, item_type, features, probability))
        if not entries:
            return []
        entries.sort(key=lambda item: abs(item[4] - 0.5))
        top = entries[: self.settings.active.top_n]

        feature_names: List[str]
        if self.feature_names:
            feature_names = list(self.feature_names)
        else:
            keys = set()
            for _path, _conf, _typ, features, _prob in top:
                keys.update(features.keys())
            feature_names = sorted(keys)

        vectors = [self._vectorize(features, feature_names) for _, _, _, features, _ in top]

        selected: List[int] = []
        if not vectors:
            return []
        selected.append(0)
        while len(selected) < min(limit, len(top)):
            best_idx = None
            best_score = -1.0
            for idx, vector in enumerate(vectors):
                if idx in selected:
                    continue
                distances = [np.linalg.norm(vector - vectors[s]) for s in selected]
                if not distances:
                    min_dist = 0.0
                else:
                    min_dist = float(min(distances))
                # prefer items with high distance but also high uncertainty
                uncertainty = abs(top[idx][4] - 0.5)
                score = -uncertainty + min_dist
                if score > best_score:
                    best_score = score
                    best_idx = idx
            if best_idx is None:
                break
            selected.append(best_idx)

        results: List[ActiveItem] = []
        for idx in selected:
            path, conf, item_type, features, probability = top[idx]
            results.append(
                ActiveItem(
                    path=path,
                    features=features,
                    p_correct=max(0.0, min(1.0, probability)),
                    uncertainty=abs(probability - 0.5),
                    source_confidence=conf,
                    item_type=item_type,
                    reasons=self._format_reasons(features),
                )
            )
        results.sort(key=lambda item: item.uncertainty)
        return results[:limit]


__all__ = [
    "ActiveItem",
    "LearningEngine",
    "LearningExamplePayload",
]

