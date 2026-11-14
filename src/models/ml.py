from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import balanced_accuracy_score
from sklearn.pipeline import Pipeline

from src.features.structure import add_structure_features
from src.features.technical import MODEL_FEATURES, add_technical_features

from .calibration import MulticlassIsotonicCalibrator, probability_metrics
from .rules import ComponentProbability
from .targets import triple_barrier_labels


STRUCTURE_MODEL_FEATURES = [
    "hh", "hl", "lh", "ll", "bull_bos", "bear_bos", "bull_choch", "bear_choch",
    "structure_score", "distance_resistance", "distance_support", "breakout_up", "breakout_down",
    "false_breakout_up", "false_breakout_down", "range_regime", "trend_strength_pa",
    "trend_acceleration", "distance_recent_high", "distance_recent_low",
]
FEATURE_COLUMNS = MODEL_FEATURES + STRUCTURE_MODEL_FEATURES


@dataclass
class ModelArtifact:
    coin: str
    horizon: str
    feature_columns: list[str]
    pipeline: Pipeline
    calibrator: MulticlassIsotonicCalibrator
    metrics: dict[str, Any]
    trained_at: int
    training_period: dict[str, int] = field(default_factory=dict)
    model_version: str = "2.0"
    target_spec: dict[str, Any] = field(default_factory=dict)
    ensemble_weights: dict[str, float] = field(default_factory=dict)
    feature_importance: dict[str, float] = field(default_factory=dict)

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        row = frame[self.feature_columns].iloc[[-1]]
        return self.predict_frame(row)[0]

    def predict_frame(self, frame: pd.DataFrame) -> np.ndarray:
        raw = self.pipeline.predict_proba(frame[self.feature_columns])
        expanded = _expand_probabilities(raw, self.pipeline.classes_)
        return self.calibrator.predict_proba(expanded)


def _expand_probabilities(probabilities: np.ndarray, classes: np.ndarray) -> np.ndarray:
    result = np.full((len(probabilities), 3), 1e-6, dtype=float)
    for index, cls in enumerate(classes):
        result[:, int(cls)] = probabilities[:, index]
    return result / result.sum(axis=1, keepdims=True)


def prepare_feature_frame(candles: pd.DataFrame) -> pd.DataFrame:
    return add_structure_features(add_technical_features(candles))


def chronological_slices(length: int, embargo_steps: int = 0) -> dict[str, slice]:
    if length < 100:
        raise ValueError("At least 100 labelled observations are required")
    train_end = int(length * 0.60)
    validation_end = int(length * 0.75)
    calibration_end = int(length * 0.90)
    embargo = max(0, int(embargo_steps))
    slices = {
        "train": slice(0, max(0, train_end - embargo)),
        "validation": slice(train_end, max(train_end, validation_end - embargo)),
        "calibration": slice(validation_end, max(validation_end, calibration_end - embargo)),
        "test": slice(calibration_end, length),
    }
    if any(value.stop <= value.start for value in slices.values()):
        raise ValueError("Insufficient observations after chronological embargo")
    return slices


def _new_pipeline(random_state: int = 42) -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("model", HistGradientBoostingClassifier(
            learning_rate=0.05, max_iter=180, max_leaf_nodes=15, min_samples_leaf=30,
            l2_regularization=1.0, early_stopping=False, random_state=random_state,
        )),
    ])


def walk_forward_balanced_accuracy(x: pd.DataFrame, y: np.ndarray, splits: int = 3, random_state: int = 42, embargo_steps: int = 0) -> list[float]:
    scores: list[float] = []
    initial = int(len(x) * 0.5)
    fold = max(30, (len(x) - initial) // splits)
    for index in range(splits):
        train_end = initial + index * fold
        test_end = min(len(x), train_end + fold)
        purged_train_end = max(0, train_end - embargo_steps)
        if test_end - train_end < 20 or len(np.unique(y[:purged_train_end])) < 2:
            continue
        model = _new_pipeline(random_state + index)
        model.fit(x.iloc[:purged_train_end], y[:purged_train_end])
        scores.append(float(balanced_accuracy_score(y[train_end:test_end], model.predict(x.iloc[train_end:test_end]))))
    return scores


def train_model(
    coin: str,
    horizon: str,
    candles: pd.DataFrame,
    horizon_steps: int,
    output_path: str | Path,
    min_samples: int = 500,
    min_class_samples: int = 30,
    atr_multiplier: float = 1.25,
    random_state: int = 42,
    walk_forward_splits: int = 3,
) -> ModelArtifact:
    features = prepare_feature_frame(candles)
    labels = triple_barrier_labels(features, horizon_steps, atr_multiplier)
    valid = labels.notna() & features[FEATURE_COLUMNS].notna().sum(axis=1).ge(len(FEATURE_COLUMNS) // 2)
    x = features.loc[valid, FEATURE_COLUMNS].reset_index(drop=True)
    y = labels.loc[valid].astype(int).to_numpy()
    if len(x) < min_samples:
        raise ValueError(f"MODEL_WARMING_UP: {coin}/{horizon}: {len(x)} < {min_samples} labelled samples")
    counts = np.bincount(y, minlength=3)
    if counts.min() < min_class_samples:
        raise ValueError(f"MODEL_WARMING_UP: {coin}/{horizon}: class counts {counts.tolist()}")
    splits = chronological_slices(len(x), horizon_steps)
    pipeline = _new_pipeline(random_state)
    pipeline.fit(x.iloc[splits["train"]], y[splits["train"]])
    # Validation is strictly chronological and reported; it is not reused for calibration.
    validation_score = float(balanced_accuracy_score(y[splits["validation"]], pipeline.predict(x.iloc[splits["validation"]])))
    calibration_raw = _expand_probabilities(pipeline.predict_proba(x.iloc[splits["calibration"]]), pipeline.classes_)
    calibrator = MulticlassIsotonicCalibrator().fit(calibration_raw, y[splits["calibration"]])
    test_raw = _expand_probabilities(pipeline.predict_proba(x.iloc[splits["test"]]), pipeline.classes_)
    test_probabilities = calibrator.predict_proba(test_raw)
    training_prior = np.bincount(y[splits["train"]], minlength=3) / len(y[splits["train"]])
    metrics = probability_metrics(test_probabilities, y[splits["test"]], training_prior)
    metrics["validation_balanced_accuracy"] = validation_score
    metrics["walk_forward_balanced_accuracy"] = walk_forward_balanced_accuracy(x.iloc[:splits["calibration"].stop], y[:splits["calibration"].stop], walk_forward_splits, random_state, horizon_steps)
    metrics["samples"] = len(x)
    metrics["class_counts"] = counts.tolist()
    training_period = {
        "start_ms": int(candles.loc[valid, "open_time"].iloc[splits["train"].start]),
        "end_ms": int(candles.loc[valid, "open_time"].iloc[splits["train"].stop - 1]),
    }
    artifact = ModelArtifact(
        coin, horizon, FEATURE_COLUMNS, pipeline, calibrator, metrics, int(time.time() * 1000),
        training_period=training_period,
        target_spec={"horizon_steps": horizon_steps, "atr_multiplier": atr_multiplier, "barrier_mode": "atr"},
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, output)
    output.with_suffix(".metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    return artifact


class ModelRegistry:
    def __init__(self, directory: str | Path, min_balanced_accuracy: float = 0.36, max_brier: float = 0.75, min_brier_skill: float = 0.0):
        self.directory = Path(directory)
        self.min_balanced_accuracy = min_balanced_accuracy
        self.max_brier = max_brier
        self.min_brier_skill = min_brier_skill
        self.artifacts: dict[tuple[str, str], ModelArtifact] = {}

    def load(self, coin: str, horizon: str) -> bool:
        path = self.directory / f"{coin}_{horizon}.joblib"
        if not path.exists():
            return False
        artifact = joblib.load(path)
        if not isinstance(artifact, ModelArtifact):
            return False
        model = artifact.pipeline.named_steps.get("model")
        # Reject legacy artifacts trained with sklearn's random internal
        # validation split. They must never enter a chronological ensemble.
        if not isinstance(model, HistGradientBoostingClassifier) or model.early_stopping is not False:
            return False
        metrics = artifact.metrics
        if (metrics.get("balanced_accuracy", 0) < self.min_balanced_accuracy or
                metrics.get("brier", 999) > self.max_brier or
                metrics.get("brier_skill", -999) <= self.min_brier_skill):
            return False
        self.artifacts[(coin, horizon)] = artifact
        return True

    def component(self, coin: str, horizon: str, frame: pd.DataFrame) -> ComponentProbability:
        artifact = self.artifacts.get((coin, horizon))
        if artifact is None:
            return ComponentProbability((0.24, 0.24, 0.52), 0.0, ("ML MODEL_WARMING_UP",))
        try:
            probs = artifact.predict(frame)
            # Admission already enforces honest OOS skill. Audited ensemble
            # weights must not be silently distorted by a second heuristic score.
            return ComponentProbability(tuple(probs.tolist()), 1.0, (f"ML {horizon} calibrated",))
        except Exception:
            return ComponentProbability((0.24, 0.24, 0.52), 0.0, ("ML unavailable",))

    def audited_weights(self, coin: str, horizon: str) -> dict[str, float] | None:
        artifact = self.artifacts.get((coin, horizon))
        weights = getattr(artifact, "ensemble_weights", None) if artifact is not None else None
        if not isinstance(weights, dict) or not weights or not math.isclose(sum(float(x) for x in weights.values()), 1.0, abs_tol=1e-8):
            return None
        aliases = {"btc": "btc_context"}
        return {aliases.get(str(name), str(name)): float(value) for name, value in weights.items()}
