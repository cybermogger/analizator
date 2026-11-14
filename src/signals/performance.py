from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping

import numpy as np


HORIZON_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
LABEL_INDEX = {"LONG": 0, "SHORT": 1, "WAIT": 2}
INDEX_LABEL = {value: key for key, value in LABEL_INDEX.items()}
CALIBRATION_BUCKETS = ((40, 50), (50, 60), (60, 70), (70, 80), (80, 101))


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("payload_json")
    return json.loads(value) if isinstance(value, str) else dict(value or {})


def _balanced_accuracy(actual: np.ndarray, predicted: np.ndarray) -> float | None:
    recalls = [float((predicted[actual == label] == label).mean()) for label in range(3) if np.any(actual == label)]
    return float(np.mean(recalls)) if recalls else None


def summarize_outcomes(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    material = list(rows)
    if not material:
        return {
            "samples": 0, "directional_accuracy": None, "balanced_accuracy": None,
            "brier": None, "ece": None, "precision_long": None, "precision_short": None,
            "average_forward_return_long": None, "average_forward_return_short": None,
            "calibration_buckets": [], "by_regime": {},
        }
    probabilities: list[list[float]] = []
    actual: list[int] = []
    regimes: list[str] = []
    returns: list[float] = []
    predictions: list[int] = []
    for row in material:
        signal = _payload(row)
        primary = signal["horizons"][signal["primary_horizon"]]
        vector = np.asarray([primary["LONG"], primary["SHORT"], primary["WAIT"]], dtype=float) / 100.0
        vector = np.clip(vector, 0, 1)
        vector /= vector.sum()
        probabilities.append(vector.tolist())
        label = LABEL_INDEX[str(row["actual_label"])]
        actual.append(label)
        predictions.append(int(np.argmax(vector)))
        regimes.append(str(signal.get("regime", "UNKNOWN")))
        returns.append(float(row["forward_return"]))
    p = np.asarray(probabilities)
    y = np.asarray(actual, dtype=int)
    pred = np.asarray(predictions, dtype=int)
    one_hot = np.eye(3)[y]
    brier = float(np.mean(np.sum((p - one_hot) ** 2, axis=1)))
    directional_mask = pred != LABEL_INDEX["WAIT"]
    directional_accuracy = float((pred[directional_mask] == y[directional_mask]).mean()) if directional_mask.any() else None

    precision: dict[int, float | None] = {}
    for label in (LABEL_INDEX["LONG"], LABEL_INDEX["SHORT"]):
        selected = pred == label
        precision[label] = float((y[selected] == label).mean()) if selected.any() else None

    bucket_report: list[dict[str, Any]] = []
    weighted_error = 0.0
    directional_choice = np.where(p[:, 0] >= p[:, 1], 0, 1)
    confidence = p[np.arange(len(p)), directional_choice] * 100.0
    success = directional_choice == y
    for lower, upper in CALIBRATION_BUCKETS:
        selected = (confidence >= lower) & (confidence < upper)
        mean_probability = float(confidence[selected].mean()) if selected.any() else None
        success_rate = float(success[selected].mean() * 100.0) if selected.any() else None
        if selected.any():
            weighted_error += selected.mean() * abs(mean_probability - success_rate) / 100.0
        bucket_report.append({
            "bucket": "80+" if lower == 80 else f"{lower}-{upper}",
            "count": int(selected.sum()),
            "predicted_probability": mean_probability,
            "success_rate": success_rate,
        })

    return_array = np.asarray(returns, dtype=float)
    by_regime: dict[str, Any] = {}
    for regime in sorted(set(regimes)):
        selected = np.asarray([value == regime for value in regimes])
        by_regime[regime] = {
            "samples": int(selected.sum()),
            "directional_accuracy": float((pred[selected] == y[selected]).mean()),
            "brier": float(np.mean(np.sum((p[selected] - one_hot[selected]) ** 2, axis=1))),
        }
    long_selected = pred == LABEL_INDEX["LONG"]
    short_selected = pred == LABEL_INDEX["SHORT"]
    return {
        "samples": len(material),
        "directional_accuracy": directional_accuracy,
        "balanced_accuracy": _balanced_accuracy(y, pred),
        "brier": brier,
        "ece": weighted_error,
        "precision_long": precision[LABEL_INDEX["LONG"]],
        "precision_short": precision[LABEL_INDEX["SHORT"]],
        "average_forward_return_long": float(return_array[long_selected].mean()) if long_selected.any() else None,
        "average_forward_return_short": float(return_array[short_selected].mean()) if short_selected.any() else None,
        "calibration_buckets": bucket_report,
        "by_regime": by_regime,
    }


class ForecastPerformance:
    def __init__(self, repository: Any, settings: Mapping[str, Any]):
        self.repository = repository
        self.settings = settings

    def evaluate_due(self, now_ms: int, limit: int = 500) -> int:
        evaluated = 0
        for row in self.repository.pending_signal_outcomes(now_ms, limit):
            signal = _payload(row)
            horizon = str(signal.get("primary_horizon", row["primary_horizon"]))
            due_timestamp = int(row["timestamp"]) + HORIZON_MS[horizon]
            if due_timestamp > now_ms:
                continue
            outcome = self.repository.context_at_or_after(str(row["coin"]), due_timestamp, due_timestamp + 900_000)
            if outcome is None:
                continue
            start_price = float(signal.get("mark", 0))
            if start_price <= 0:
                continue
            forward_return = float(outcome["mark"]) / start_price - 1.0
            threshold = max(0.0001, float(signal.get("outcome_threshold_pct", 0.1)) / 100.0)
            actual_label = "LONG" if forward_return >= threshold else "SHORT" if forward_return <= -threshold else "WAIT"
            primary = signal["horizons"][horizon]
            predicted_label = max(("LONG", "SHORT", "WAIT"), key=lambda key: primary[key])
            self.repository.save_signal_outcome(
                int(row["id"]), str(row["coin"]), horizon, due_timestamp, int(outcome["timestamp"]),
                float(outcome["mark"]), forward_return, actual_label, predicted_label == actual_label,
            )
            evaluated += 1
        return evaluated

    def rows(self, coin: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        return self.repository.evaluated_signal_rows(coin, limit)

    def summary(self, coin: str | None = None, limit: int = 5000) -> dict[str, Any]:
        return summarize_outcomes(self.rows(coin, limit))

    def runtime_confidence_scale(self, coin: str, horizon: str) -> float:
        minimum = int(self.settings.get("minimum_samples", 100))
        rows = [row for row in self.rows(coin) if _payload(row).get("primary_horizon") == horizon]
        if len(rows) < minimum:
            return 1.0
        confidences: list[float] = []
        successes: list[float] = []
        for row in rows:
            signal = _payload(row)
            probabilities = signal["horizons"][horizon]
            direction = "LONG" if probabilities["LONG"] >= probabilities["SHORT"] else "SHORT"
            confidence = float(probabilities[direction]) / 100.0
            if confidence >= 0.40:
                confidences.append(confidence)
                successes.append(float(str(row["actual_label"]) == direction))
        if len(confidences) < minimum:
            return 1.0
        overconfidence = float(np.mean(confidences) - np.mean(successes))
        tolerance = float(self.settings.get("overconfidence_tolerance", 0.05))
        max_shift = float(self.settings.get("max_wait_shift", 0.20))
        return float(np.clip(1.0 - max(0.0, overconfidence - tolerance), 1.0 - max_shift, 1.0))

    def component_multipliers(self, coin: str, horizon: str, regime: str, names: Iterable[str]) -> dict[str, float]:
        result = {name: 1.0 for name in names}
        minimum = int(self.settings.get("minimum_samples", 100))
        rows = [
            row for row in self.rows(coin)
            if _payload(row).get("primary_horizon") == horizon and _payload(row).get("regime") == regime
        ]
        if len(rows) < minimum:
            return result
        bounds = self.settings.get("weight_multiplier_bounds", [0.8, 1.2])
        lower, upper = float(bounds[0]), float(bounds[1])
        regularization = max(1.0, float(self.settings.get("regularization_samples", 300)))
        maximum_adjustment = float(self.settings.get("max_weight_adjustment", 0.15))
        shrinkage = len(rows) / (len(rows) + regularization)
        actual = np.asarray([LABEL_INDEX[str(row["actual_label"])] for row in rows], dtype=int)
        one_hot = np.eye(3)[actual]
        ensemble_vectors = []
        for row in rows:
            signal = _payload(row)
            values = signal["horizons"][horizon]
            ensemble_vectors.append([values["LONG"], values["SHORT"], values["WAIT"]])
        ensemble = np.asarray(ensemble_vectors, dtype=float) / 100.0
        ensemble_brier = float(np.mean(np.sum((ensemble - one_hot) ** 2, axis=1)))
        for name in result:
            vectors: list[list[float]] = []
            labels: list[int] = []
            for index, row in enumerate(rows):
                signal = _payload(row)
                component = signal.get("details", {}).get(horizon, {}).get("components", {}).get(name)
                if component and float(component.get("quality", 0)) > 0:
                    values = component["probabilities"]
                    vectors.append([values["LONG"] / 100.0, values["SHORT"] / 100.0, values["WAIT"] / 100.0])
                    labels.append(index)
            if len(vectors) < minimum:
                continue
            component_p = np.asarray(vectors)
            component_y = one_hot[np.asarray(labels)]
            component_brier = float(np.mean(np.sum((component_p - component_y) ** 2, axis=1)))
            relative = (ensemble_brier - component_brier) / max(ensemble_brier, 1e-9)
            adjustment = float(np.clip(relative * shrinkage, -maximum_adjustment, maximum_adjustment))
            result[name] = float(np.clip(1.0 + adjustment, lower, upper))
        return result
