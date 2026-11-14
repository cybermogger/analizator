from __future__ import annotations

import numpy as np
import joblib

from src.models.calibration import MulticlassIsotonicCalibrator, calibration_report, probability_metrics
from src.models.ml import ModelRegistry, chronological_slices, prepare_feature_frame, train_model


def test_calibration_normalizes_and_reports():
    rng = np.random.default_rng(2)
    raw = rng.dirichlet([2, 2, 3], 300)
    labels = np.array([rng.choice(3, p=p) for p in raw])
    calibrator = MulticlassIsotonicCalibrator().fit(raw[:200], labels[:200])
    calibrated = calibrator.predict_proba(raw[200:])
    assert np.allclose(calibrated.sum(axis=1), 1)
    metrics = probability_metrics(calibrated, labels[200:])
    assert {"brier", "log_loss", "ece", "balanced_accuracy", "calibration_report"}.issubset(metrics)


def test_directional_calibration_buckets_are_exact():
    probabilities = np.array([[0.42, 0.20, 0.38], [0.51, 0.25, 0.24], [0.10, 0.81, 0.09]])
    report = calibration_report(probabilities, np.array([0, 2, 1]))
    assert [row["bucket"] for row in report] == ["40-45%", "45-50%", "50-55%", "55-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80+%"]
    assert report[0]["count"] == 1 and report[0]["actual_success_rate"] == 1
    assert report[2]["count"] == 1 and report[2]["actual_success_rate"] == 0
    assert report[-1]["count"] == 1 and report[-1]["actual_success_rate"] == 1


def test_chronological_splits_do_not_overlap():
    slices = chronological_slices(1000, embargo_steps=4)
    assert slices["train"].stop <= slices["validation"].start
    assert slices["validation"].stop <= slices["calibration"].start
    assert slices["calibration"].stop <= slices["test"].start
    assert slices["validation"].start - slices["train"].stop == 4
    assert slices["calibration"].start - slices["validation"].stop == 4
    assert slices["test"].start - slices["calibration"].stop == 4


def test_ml_pipeline_roundtrip(candles, tmp_path):
    artifact = train_model("SOL", "15m", candles, 3, tmp_path / "SOL_15m.joblib", min_samples=300, min_class_samples=10, walk_forward_splits=1)
    assert artifact.pipeline.named_steps["model"].early_stopping is False
    features = prepare_feature_frame(candles)
    probabilities = artifact.predict(features)
    assert np.isclose(probabilities.sum(), 1)
    assert artifact.model_version == "2.0"
    assert artifact.training_period["start_ms"] < artifact.training_period["end_ms"]
    assert artifact.target_spec["horizon_steps"] == 3
    artifact.ensemble_weights = {"ml": 0.7, "btc": 0.3}
    joblib.dump(artifact, tmp_path / "SOL_15m.joblib")
    registry = ModelRegistry(tmp_path, min_balanced_accuracy=0, max_brier=2, min_brier_skill=-999)
    assert registry.load("SOL", "15m")
    assert registry.component("SOL", "15m", features).quality > 0
    assert registry.audited_weights("SOL", "15m") == {"ml": 0.7, "btc_context": 0.3}


def test_ml_cold_start_has_zero_quality_and_safe_prior(candles, tmp_path):
    registry = ModelRegistry(tmp_path / "empty")
    component = registry.component("SOL", "15m", prepare_feature_frame(candles))
    assert component.quality == 0
    assert component.probabilities == (0.24, 0.24, 0.52)
    assert sum(component.probabilities) == 1


def test_registry_rejects_legacy_random_internal_validation(candles, tmp_path):
    path = tmp_path / "SOL_15m.joblib"
    artifact = train_model("SOL", "15m", candles, 3, path, min_samples=300, min_class_samples=10, walk_forward_splits=1)
    artifact.pipeline.named_steps["model"].early_stopping = "auto"
    joblib.dump(artifact, path)
    registry = ModelRegistry(tmp_path, min_balanced_accuracy=0, max_brier=2, min_brier_skill=-999)
    assert registry.load("SOL", "15m") is False
