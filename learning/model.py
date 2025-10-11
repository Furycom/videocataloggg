"""Model training and inference helpers for the learning subsystem."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - optional dependency during runtime
    import onnxruntime as ort
except Exception:  # pragma: no cover - fallback
    ort = None

from sklearn.calibration import CalibratedClassifierCV
from sklearn.exceptions import NotFittedError
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction import DictVectorizer

try:  # pragma: no cover - optional conversion dependency
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import DictionaryType, FloatTensorType, StringTensorType
except Exception:  # pragma: no cover
    convert_sklearn = None

try:  # pragma: no cover - joblib provided by scikit-learn
    from joblib import dump, load as joblib_load
except Exception:  # pragma: no cover
    joblib_load = None
    dump = None

from .config import LearningSettings
from .db import LearningExample

LOGGER = logging.getLogger("videocatalog.learning")


@dataclass(slots=True)
class ReliabilityBin:
    lower: float
    upper: float
    accuracy: float
    mean_confidence: float
    count: int


@dataclass(slots=True)
class TrainingArtifacts:
    pipeline: Pipeline
    calibrator: str
    metrics: Dict[str, float]
    reliability: List[ReliabilityBin]
    feature_names: List[str]
    version: str
    onnx_path: Optional[Path]
    storage_path: Optional[Path]


def _dict_to_array(data: Sequence[Mapping[str, float]]) -> List[Mapping[str, float]]:
    return [dict(item) for item in data]


def _calibration_method(settings: LearningSettings, n_samples: int) -> str:
    mode = settings.calibration
    if mode == "auto":
        return "isotonic" if n_samples >= 1000 else "sigmoid"
    if mode in {"sigmoid", "isotonic"}:
        return mode
    return "none"


def _compute_reliability(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> List[ReliabilityBin]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    results: List[ReliabilityBin] = []
    for idx in range(bins):
        lower, upper = edges[idx], edges[idx + 1]
        mask = (y_prob >= lower) & (y_prob < upper if idx < bins - 1 else y_prob <= upper)
        if not np.any(mask):
            results.append(ReliabilityBin(lower, upper, 0.0, 0.0, 0))
            continue
        bin_true = y_true[mask]
        bin_prob = y_prob[mask]
        accuracy = float(np.mean(bin_true)) if bin_true.size else 0.0
        mean_conf = float(np.mean(bin_prob)) if bin_prob.size else 0.0
        results.append(
            ReliabilityBin(lower=lower, upper=upper, accuracy=accuracy, mean_confidence=mean_conf, count=int(mask.sum()))
        )
    return results


def _expected_calibration_error(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> float:
    reliability = _compute_reliability(y_true, y_prob, bins)
    total = len(y_true)
    if total == 0:
        return 0.0
    error = 0.0
    for bin_info in reliability:
        weight = bin_info.count / total
        error += weight * abs(bin_info.accuracy - bin_info.mean_confidence)
    return float(error)


def _prepare_pipeline(settings: LearningSettings, calibration: str) -> Pipeline:
    vectorizer = DictVectorizer(sparse=False)

    if settings.algo != "logreg":
        raise ValueError(f"Unsupported learning algo: {settings.algo}")

    class_weight = None if settings.class_weight == "none" else settings.class_weight
    base = LogisticRegression(
        solver="liblinear",
        class_weight=class_weight,
        max_iter=400,
    )
    if calibration == "none":
        steps = [("vectorizer", vectorizer), ("clf", base)]
    else:
        calibrated = CalibratedClassifierCV(
            base_estimator=base,
            method=calibration,
            cv=max(2, settings.k_folds),
        )
        steps = [("vectorizer", vectorizer), ("calibrator", calibrated)]
    return Pipeline(steps)


def train_model(
    examples: Sequence[LearningExample],
    *,
    settings: LearningSettings,
    working_dir: Path,
) -> Optional[TrainingArtifacts]:
    if len(examples) < 2:
        LOGGER.warning("Not enough labeled examples to train: %d", len(examples))
        return None

    labels = np.array([ex.label for ex in examples], dtype=np.int32)
    features = _dict_to_array([ex.features for ex in examples])

    calibration = _calibration_method(settings, len(examples))
    pipeline = _prepare_pipeline(settings, calibration)

    pipeline.fit(features, labels)

    try:
        probabilities = pipeline.predict_proba(features)[:, 1]
    except AttributeError:
        # Uncalibrated logistic regression exposes predict_proba on base estimator
        probabilities = pipeline.named_steps["clf"].predict_proba(
            pipeline.named_steps["vectorizer"].transform(features)
        )[:, 1]

    auc = float(roc_auc_score(labels, probabilities)) if len(set(labels)) > 1 else 0.0
    brier = float(brier_score_loss(labels, probabilities))
    ece = _expected_calibration_error(labels, probabilities)
    reliability = _compute_reliability(labels, probabilities)

    feature_names = list(pipeline.named_steps["vectorizer"].get_feature_names_out())

    models_dir = working_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    version = f"logreg-{calibration}-{len(examples)}"
    onnx_path: Optional[Path] = None
    storage_path: Optional[Path] = None

    if convert_sklearn is not None:
        try:
            initial_types = [
                ("input", DictionaryType(StringTensorType([None, 1]), FloatTensorType([None, 1])))
            ]
            onnx_model = convert_sklearn(pipeline, initial_types=initial_types)
            onnx_path = models_dir / "confidence-calibrated.onnx"
            with open(onnx_path, "wb") as handle:
                handle.write(onnx_model.SerializeToString())
        except Exception as exc:  # pragma: no cover - runtime guard
            LOGGER.warning("Failed to export ONNX model: %s", exc)
            onnx_path = None

    if dump is not None:
        try:
            storage_path = models_dir / "confidence-calibrated.joblib"
            dump(pipeline, storage_path)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Failed to persist sklearn model: %s", exc)
            storage_path = None

    metrics = {
        "auc": auc,
        "brier": brier,
        "ece": ece,
        "n": len(examples),
        "calibration": calibration,
    }

    return TrainingArtifacts(
        pipeline=pipeline,
        calibrator=calibration,
        metrics=metrics,
        reliability=reliability,
        feature_names=feature_names,
        version=version,
        onnx_path=onnx_path,
        storage_path=storage_path,
    )


class OnnxPipeline:
    """Thin wrapper that mimics a sklearn pipeline using onnxruntime."""

    def __init__(self, session: "ort.InferenceSession", feature_names: Sequence[str]) -> None:
        self.session = session
        self.feature_names = list(feature_names)

    def predict_proba(self, features: Sequence[Mapping[str, float]]) -> np.ndarray:
        if not features:
            return np.zeros((0, 2), dtype=np.float32)
        inputs = [{name: float(item.get(name, 0.0)) for name in self.feature_names} for item in features]
        # onnxruntime expects dict of numpy arrays keyed by the model input name
        input_name = self.session.get_inputs()[0].name
        output_name = self.session.get_outputs()[0].name
        ort_inputs = {input_name: inputs}
        result = self.session.run([output_name], ort_inputs)[0]
        return np.asarray(result, dtype=np.float32)


def load_pipeline(working_dir: Path, feature_names: Sequence[str]) -> Optional[object]:
    models_dir = working_dir / "models"
    onnx_path = models_dir / "confidence-calibrated.onnx"
    if onnx_path.exists() and ort is not None:
        try:
            session = ort.InferenceSession(str(onnx_path), providers=None)
            return OnnxPipeline(session, feature_names)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Failed to load ONNX model: %s", exc)
    joblib_path = models_dir / "confidence-calibrated.joblib"
    if joblib_path.exists() and joblib_load is not None:
        try:
            return joblib_load(joblib_path)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Failed to load sklearn model: %s", exc)
    return None


def score_items(
    pipeline: object,
    features: Sequence[Mapping[str, float]],
) -> List[float]:
    if not features:
        return []
    if hasattr(pipeline, "predict_proba"):
        probs = pipeline.predict_proba(features)
        if isinstance(probs, list):
            probs = np.asarray(probs, dtype=float)
        if probs.ndim == 1:
            return [float(p) for p in probs]
        return [float(row[1]) for row in probs]
    raise NotFittedError("Pipeline missing predict_proba")


def reliability_as_dict(bins: Sequence[ReliabilityBin]) -> List[Dict[str, float]]:
    return [
        {
            "lower": bin.lower,
            "upper": bin.upper,
            "accuracy": bin.accuracy,
            "mean_confidence": bin.mean_confidence,
            "count": bin.count,
        }
        for bin in bins
    ]


__all__ = [
    "TrainingArtifacts",
    "load_pipeline",
    "reliability_as_dict",
    "score_items",
    "train_model",
]

