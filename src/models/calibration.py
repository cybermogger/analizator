from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import balanced_accuracy_score, confusion_matrix, log_loss, precision_recall_fscore_support


@dataclass
class MulticlassIsotonicCalibrator:
    classes: np.ndarray = field(default_factory=lambda: np.array([0, 1, 2]))
    calibrators: dict[int, IsotonicRegression] = field(default_factory=dict)

    def fit(self, probabilities: np.ndarray, labels: np.ndarray) -> "MulticlassIsotonicCalibrator":
        for index, cls in enumerate(self.classes):
            binary = (labels == cls).astype(int)
            if len(np.unique(binary)) < 2:
                continue
            model = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
            model.fit(probabilities[:, index], binary)
            self.calibrators[int(cls)] = model
        return self

    def predict_proba(self, probabilities: np.ndarray) -> np.ndarray:
        calibrated = np.asarray(probabilities, dtype=float).copy()
        for index, cls in enumerate(self.classes):
            model = self.calibrators.get(int(cls))
            if model is not None:
                calibrated[:, index] = model.predict(probabilities[:, index])
        calibrated = np.clip(calibrated, 1e-6, None)
        return calibrated / calibrated.sum(axis=1, keepdims=True)


def expected_calibration_error(probabilities: np.ndarray, labels: np.ndarray, bins: int = 10) -> float:
    predictions = probabilities.argmax(axis=1)
    confidence = probabilities.max(axis=1)
    correct = predictions == labels
    ece = 0.0
    for lower in np.linspace(0, 1, bins + 1)[:-1]:
        upper = lower + 1 / bins
        selected = (confidence >= lower) & (confidence < upper if upper < 1 else confidence <= upper)
        if selected.any():
            ece += selected.mean() * abs(float(correct[selected].mean()) - float(confidence[selected].mean()))
    return float(ece)


def calibration_report(probabilities: np.ndarray, labels: np.ndarray) -> list[dict[str, Any]]:
    """Directional signal reliability in the alert-relevant probability buckets."""
    report: list[dict[str, Any]] = []
    directional = probabilities[:, :2]
    predicted_direction = directional.argmax(axis=1)
    confidence = directional.max(axis=1)
    edges = (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.0000001)
    for lower, upper in zip(edges, edges[1:]):
        selected = (confidence >= lower) & (confidence < upper)
        bucket = f"{int(lower*100)}+%" if upper > 1 else f"{int(lower*100)}-{int(upper*100)}%"
        report.append({
            "bucket": bucket, "count": int(selected.sum()),
            "predicted_probability": float(confidence[selected].mean()) if selected.any() else None,
            "actual_success_rate": float((predicted_direction[selected] == labels[selected]).mean()) if selected.any() else None,
        })
    return report


def probability_metrics(
    probabilities: np.ndarray,
    labels: np.ndarray,
    reference_prior: np.ndarray | list[float] | tuple[float, float, float] | None = None,
) -> dict[str, Any]:
    predictions = probabilities.argmax(axis=1)
    one_hot = np.eye(3)[labels.astype(int)]
    precision, recall, f1, support = precision_recall_fscore_support(labels, predictions, labels=[0, 1, 2], zero_division=0)
    brier = float(np.mean(np.sum((probabilities - one_hot) ** 2, axis=1)))
    prior = np.asarray(reference_prior, dtype=float) if reference_prior is not None else one_hot.mean(axis=0)
    prior = np.clip(prior, 1e-9, None)
    prior /= prior.sum()
    baseline = np.tile(prior, (len(labels), 1))
    baseline_brier = float(np.mean(np.sum((baseline - one_hot) ** 2, axis=1)))
    return {
        "balanced_accuracy": float(balanced_accuracy_score(labels, predictions)),
        "brier": brier, "baseline_brier": baseline_brier,
        "brier_skill": float(1 - brier / baseline_brier) if baseline_brier > 0 else 0.0,
        "log_loss": float(log_loss(labels, probabilities, labels=[0, 1, 2])),
        "ece": expected_calibration_error(probabilities, labels),
        "precision": precision.tolist(), "recall": recall.tolist(), "f1": f1.tolist(), "support": support.tolist(),
        "precision_long": float(precision[0]), "precision_short": float(precision[1]),
        "recall_long": float(recall[0]), "recall_short": float(recall[1]),
        "f1_long": float(f1[0]), "f1_short": float(f1[1]), "f1_macro": float(np.mean(f1)),
        "confusion_matrix": confusion_matrix(labels, predictions, labels=[0, 1, 2]).tolist(),
        "calibration_report": calibration_report(probabilities, labels),
    }
