from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.data.repository import MarketRepository
from src.data.storage import SQLiteStorage
from src.features.technical import MODEL_FEATURES
from src.models.calibration import MulticlassIsotonicCalibrator, expected_calibration_error, probability_metrics
from src.models.rules import directional_distribution, structure_component, technical_component
from src.service.config import load_config

from .ml import (
    FEATURE_COLUMNS, STRUCTURE_MODEL_FEATURES, ModelArtifact, _expand_probabilities,
    _new_pipeline, prepare_feature_frame,
)
from .targets import triple_barrier_labels


MODEL_VERSION = "quant-2.0"
BTC_FEATURES = [
    "btc_trend_score", "btc_momentum_score", "btc_rsi_14", "btc_atr_pct",
    "btc_realized_vol_20", "btc_structure_score", "asset_btc_corr_60", "asset_btc_beta_60",
]


@dataclass(frozen=True)
class BarrierSpec:
    name: str
    atr_multiplier: float
    volatility_multiplier: float = 0.0
    mode: str = "atr"
    use_structure: bool = True


DEFAULT_BARRIER_SPECS = (
    BarrierSpec("atr_0.90", 0.90),
    BarrierSpec("atr_1.10", 1.10),
    BarrierSpec("atr_1.25", 1.25),
    BarrierSpec("atr_1.45", 1.45),
    BarrierSpec("blend_atr0.85_vol0.85", 0.85, 0.85, "blend"),
    BarrierSpec("max_atr0.80_vol0.80", 0.80, 0.80, "max"),
    BarrierSpec("max_atr0.90_vol1.00", 0.90, 1.00, "max"),
)


def _class_balance(labels: np.ndarray) -> dict[str, Any]:
    counts = np.bincount(labels.astype(int), minlength=3)
    fractions = counts / max(int(counts.sum()), 1)
    return {
        "counts": {"LONG": int(counts[0]), "SHORT": int(counts[1]), "WAIT": int(counts[2])},
        "fractions": {"LONG": float(fractions[0]), "SHORT": float(fractions[1]), "WAIT": float(fractions[2])},
    }


def _selection_score(metrics: dict[str, Any]) -> float:
    """Calibration-led objective; balanced accuracy cannot dominate Brier/ECE."""
    return float(
        metrics.get("brier_skill", -1.0)
        + 0.30 * (metrics.get("balanced_accuracy", 0.0) - 1 / 3)
        + 0.10 * (metrics.get("f1_macro", 0.0) - 1 / 3)
        - 0.40 * metrics.get("ece", 1.0)
    )


def _quick_score(probabilities: np.ndarray, labels: np.ndarray, prior: np.ndarray) -> float:
    one_hot = np.eye(3)[labels]
    brier = float(np.mean(np.sum((probabilities - one_hot) ** 2, axis=1)))
    baseline = float(np.mean(np.sum((prior - one_hot) ** 2, axis=1)))
    skill = 1 - brier / baseline if baseline > 0 else -1.0
    predictions = probabilities.argmax(axis=1)
    recalls = [float((predictions[labels == cls] == cls).mean()) for cls in range(3) if np.any(labels == cls)]
    balanced = float(np.mean(recalls)) if recalls else 0.0
    ece = expected_calibration_error(probabilities, labels)
    return float(skill + 0.30 * (balanced - 1 / 3) - 0.40 * ece)


def _prepare_frame(asset_candles: pd.DataFrame, btc_candles: pd.DataFrame) -> pd.DataFrame:
    asset = prepare_feature_frame(asset_candles).sort_values("open_time").reset_index(drop=True)
    if btc_candles.empty:
        for column in BTC_FEATURES:
            asset[column] = np.nan
        return asset
    btc = prepare_feature_frame(btc_candles).sort_values("open_time").reset_index(drop=True)
    btc["btc_trend_score"] = np.tanh((btc["ema_21"] / (btc["ema_50"] + 1e-12) - 1) * 100)
    btc["btc_momentum_score"] = np.tanh(btc["roc_10"] / (btc["atr_pct"] * 3 + 1e-12))
    btc["btc_rsi_14"] = btc["rsi_14"]
    btc["btc_atr_pct"] = btc["atr_pct"]
    btc["btc_realized_vol_20"] = btc["realized_vol_20"]
    btc["btc_structure_score"] = btc["structure_score"]
    btc["btc_return_1"] = btc["return_1"]
    view = btc[["open_time", "btc_return_1", *BTC_FEATURES[:6]]]
    merged = pd.merge_asof(asset, view, on="open_time", direction="backward")
    merged["asset_btc_corr_60"] = merged["return_1"].rolling(60, min_periods=30).corr(merged["btc_return_1"])
    covariance = merged["return_1"].rolling(60, min_periods=30).cov(merged["btc_return_1"])
    variance = merged["btc_return_1"].rolling(60, min_periods=30).var(ddof=0)
    merged["asset_btc_beta_60"] = covariance / (variance + 1e-12)
    return merged.replace([np.inf, -np.inf], np.nan)


def _rule_components(frame: pd.DataFrame, positions: np.ndarray, interval: str) -> dict[str, np.ndarray]:
    technical: list[tuple[float, float, float]] = []
    structure: list[tuple[float, float, float]] = []
    btc: list[tuple[float, float, float]] = []
    for position in positions:
        history = frame.iloc[max(0, position - 19):position + 1]
        technical.append(technical_component({interval: history.iloc[[-1]]}, {interval: 1.0}).probabilities)
        structure.append(structure_component({interval: history}, {interval: 1.0}).probabilities)
        row = frame.iloc[position]
        score_values = [row.get("btc_trend_score"), row.get("btc_momentum_score"), row.get("btc_structure_score")]
        finite = [float(value) for value in score_values if value is not None and np.isfinite(value)]
        btc.append(directional_distribution(float(np.mean(finite)) if finite else 0.0, 0.8, 1.0 if finite else 0.0))
    return {
        "technical": np.asarray(technical, dtype=float),
        "structure": np.asarray(structure, dtype=float),
        "btc": np.asarray(btc, dtype=float),
    }


def _correlation_prune(frame: pd.DataFrame, columns: list[str], stop: int, threshold: float = 0.97) -> tuple[list[str], list[str]]:
    train = frame.iloc[:stop][columns].replace([np.inf, -np.inf], np.nan)
    nonconstant = [column for column in columns if train[column].nunique(dropna=True) > 1]
    correlation = train[nonconstant].corr().abs()
    upper = correlation.where(np.triu(np.ones(correlation.shape), k=1).astype(bool))
    removed = [column for column in upper.columns if (upper[column] > threshold).any()]
    removed.extend(column for column in columns if column not in nonconstant)
    kept = [column for column in columns if column not in set(removed)]
    return kept, list(dict.fromkeys(removed))


def _walk_forward_ranges(length: int, horizon_steps: int, folds: int = 3) -> list[tuple[slice, slice, slice]]:
    selection_end = int(length * 0.75)
    boundaries = np.linspace(int(length * 0.45), selection_end, folds + 1, dtype=int)
    ranges: list[tuple[slice, slice, slice]] = []
    for index in range(folds):
        validation_start = int(boundaries[index])
        validation_stop = int(boundaries[index + 1])
        if index == folds - 1:
            validation_stop -= horizon_steps
        history_stop = validation_start - horizon_steps
        calibration_size = max(100, int(history_stop * 0.15))
        calibration_start = history_stop - calibration_size
        fit_stop = calibration_start - horizon_steps
        if fit_stop < 300 or validation_stop - validation_start < 30:
            continue
        ranges.append((slice(0, fit_stop), slice(calibration_start, history_stop), slice(validation_start, validation_stop)))
    if not ranges:
        raise ValueError("Insufficient observations for purged walk-forward evaluation")
    return ranges


def _fit_hgb(frame: pd.DataFrame, labels: np.ndarray, fit: slice, calibration: slice, columns: list[str], random_state: int) -> tuple[Pipeline, MulticlassIsotonicCalibrator]:
    pipeline = _new_pipeline(random_state)
    pipeline.fit(frame.iloc[fit][columns], labels[fit])
    raw = _expand_probabilities(pipeline.predict_proba(frame.iloc[calibration][columns]), pipeline.classes_)
    calibrator = MulticlassIsotonicCalibrator().fit(raw, labels[calibration])
    return pipeline, calibrator


def _hgb_probabilities(pipeline: Pipeline, calibrator: MulticlassIsotonicCalibrator, frame: pd.DataFrame, rows: slice, columns: list[str]) -> np.ndarray:
    raw = _expand_probabilities(pipeline.predict_proba(frame.iloc[rows][columns]), pipeline.classes_)
    return calibrator.predict_proba(raw)


def _fit_statistical(frame: pd.DataFrame, labels: np.ndarray, fit: slice, calibration: slice, columns: list[str]) -> tuple[Pipeline, MulticlassIsotonicCalibrator]:
    neighbours = min(75, max(20, int(math.sqrt(fit.stop - fit.start))))
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("model", KNeighborsClassifier(n_neighbors=neighbours, weights="distance")),
    ])
    pipeline.fit(frame.iloc[fit][columns], labels[fit])
    raw = _expand_probabilities(pipeline.predict_proba(frame.iloc[calibration][columns]), pipeline.classes_)
    calibrator = MulticlassIsotonicCalibrator().fit(raw, labels[calibration])
    return pipeline, calibrator


def _static_probabilities(matrix: np.ndarray, labels: np.ndarray, calibration: slice, rows: slice) -> np.ndarray:
    calibrator = MulticlassIsotonicCalibrator().fit(matrix[calibration], labels[calibration])
    return calibrator.predict_proba(matrix[rows])


def _walk_forward_ml(frame: pd.DataFrame, labels: np.ndarray, columns: list[str], horizon_steps: int, random_state: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    predictions: list[np.ndarray] = []
    outcomes: list[np.ndarray] = []
    ranges = _walk_forward_ranges(len(labels), horizon_steps)
    for fold, (fit, calibration, validation) in enumerate(ranges):
        pipeline, calibrator = _fit_hgb(frame, labels, fit, calibration, columns, random_state + fold)
        predictions.append(_hgb_probabilities(pipeline, calibrator, frame, validation, columns))
        outcomes.append(labels[validation])
    prior_stop = ranges[0][0].stop
    prior = np.bincount(labels[:prior_stop], minlength=3).astype(float)
    prior /= prior.sum()
    return np.vstack(predictions), np.concatenate(outcomes), prior


def _walk_forward_components(
    frame: pd.DataFrame,
    labels: np.ndarray,
    columns: list[str],
    static: dict[str, np.ndarray],
    horizon_steps: int,
    random_state: int,
) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    collected: dict[str, list[np.ndarray]] = {name: [] for name in ("technical", "structure", "btc", "ml", "statistical")}
    outcomes: list[np.ndarray] = []
    ranges = _walk_forward_ranges(len(labels), horizon_steps)
    for fold, (fit, calibration, validation) in enumerate(ranges):
        hgb, hgb_calibrator = _fit_hgb(frame, labels, fit, calibration, columns, random_state + fold)
        statistical, statistical_calibrator = _fit_statistical(frame, labels, fit, calibration, columns, )
        collected["ml"].append(_hgb_probabilities(hgb, hgb_calibrator, frame, validation, columns))
        statistical_raw = _expand_probabilities(statistical.predict_proba(frame.iloc[validation][columns]), statistical.classes_)
        collected["statistical"].append(statistical_calibrator.predict_proba(statistical_raw))
        for name in ("technical", "structure", "btc"):
            collected[name].append(_static_probabilities(static[name], labels, calibration, validation))
        outcomes.append(labels[validation])
    prior = np.bincount(labels[:ranges[0][0].stop], minlength=3).astype(float)
    prior /= prior.sum()
    return {name: np.vstack(values) for name, values in collected.items()}, np.concatenate(outcomes), prior


def _weight_compositions(total: int, count: int, prefix: tuple[int, ...] = ()) -> Iterable[tuple[int, ...]]:
    if count == 1:
        yield (*prefix, total)
        return
    for value in range(total + 1):
        yield from _weight_compositions(total - value, count - 1, (*prefix, value))


def _tune_weights(
    components: dict[str, np.ndarray], labels: np.ndarray, prior: np.ndarray, names: list[str], resolution: int = 10,
) -> tuple[dict[str, float], dict[str, Any]]:
    best_score = -math.inf
    best_weights: dict[str, float] = {}
    best_probabilities: np.ndarray | None = None
    stack = np.stack([components[name] for name in names])
    for integers in _weight_compositions(resolution, len(names)):
        vector = np.asarray(integers, dtype=float) / resolution
        probabilities = np.tensordot(vector, stack, axes=(0, 0))
        score = _quick_score(probabilities, labels, prior) - 1e-5 * int(np.count_nonzero(vector))
        if score > best_score:
            best_score = score
            best_weights = {name: float(weight) for name, weight in zip(names, vector, strict=True)}
            best_probabilities = probabilities
    assert best_probabilities is not None
    metrics = probability_metrics(best_probabilities, labels, prior)
    metrics["selection_score"] = _selection_score(metrics)
    return best_weights, metrics


def _select_features(
    frame: pd.DataFrame, labels: np.ndarray, horizon_steps: int, random_state: int,
) -> tuple[list[str], dict[str, Any]]:
    train_stop = int(len(labels) * 0.60) - horizon_steps
    validation = slice(int(len(labels) * 0.60), int(len(labels) * 0.75) - horizon_steps)
    correlated, removed_correlated = _correlation_prune(frame, FEATURE_COLUMNS, train_stop)
    pipeline = _new_pipeline(random_state)
    pipeline.fit(frame.iloc[:train_stop][correlated], labels[:train_stop])
    importance = permutation_importance(
        pipeline, frame.iloc[validation][correlated], labels[validation],
        scoring="balanced_accuracy", n_repeats=3, random_state=random_state, n_jobs=1,
    )
    ranked = sorted(zip(correlated, importance.importances_mean, strict=True), key=lambda item: item[1], reverse=True)
    positive = [name for name, value in ranked if value > 0]
    minimum = min(14, len(correlated))
    candidate = list(dict.fromkeys((positive[:32] if len(positive) >= minimum else [name for name, _ in ranked[:minimum]])))
    base_prob, base_y, base_prior = _walk_forward_ml(frame, labels, correlated, horizon_steps, random_state)
    selected_prob, selected_y, selected_prior = _walk_forward_ml(frame, labels, candidate, horizon_steps, random_state)
    base_metrics = probability_metrics(base_prob, base_y, base_prior)
    selected_metrics = probability_metrics(selected_prob, selected_y, selected_prior)
    choose_candidate = (
        _selection_score(selected_metrics) >= _selection_score(base_metrics) - 0.005
        and selected_metrics["brier"] <= base_metrics["brier"] + 0.005
    )
    chosen = candidate if choose_candidate else correlated
    return chosen, {
        "correlation_threshold": 0.97,
        "removed_correlated": removed_correlated,
        "removed_unimportant": [name for name in correlated if name not in candidate],
        "permutation_importance": {name: float(value) for name, value in ranked},
        "correlation_pruned_metrics": base_metrics,
        "importance_pruned_metrics": selected_metrics,
        "selected": "importance_pruned" if choose_candidate else "correlation_pruned",
        "feature_count": len(chosen),
        "features": chosen,
    }


def _final_component_predictions(
    frame: pd.DataFrame,
    labels: np.ndarray,
    columns: list[str],
    static: dict[str, np.ndarray],
    horizon_steps: int,
    random_state: int,
) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray, Pipeline, MulticlassIsotonicCalibrator]:
    selection_end = int(len(labels) * 0.75)
    calibration_end = int(len(labels) * 0.90)
    fit = slice(0, selection_end - horizon_steps)
    calibration = slice(selection_end, calibration_end - horizon_steps)
    test = slice(calibration_end, len(labels))
    hgb, hgb_calibrator = _fit_hgb(frame, labels, fit, calibration, columns, random_state)
    statistical, statistical_calibrator = _fit_statistical(frame, labels, fit, calibration, columns)
    result = {
        "ml": _hgb_probabilities(hgb, hgb_calibrator, frame, test, columns),
    }
    statistical_raw = _expand_probabilities(statistical.predict_proba(frame.iloc[test][columns]), statistical.classes_)
    result["statistical"] = statistical_calibrator.predict_proba(statistical_raw)
    for name in ("technical", "structure", "btc"):
        result[name] = _static_probabilities(static[name], labels, calibration, test)
    prior = np.bincount(labels[fit], minlength=3).astype(float)
    prior /= prior.sum()
    return result, labels[test], prior, hgb, hgb_calibrator


def _combine(components: dict[str, np.ndarray], weights: dict[str, float]) -> np.ndarray:
    probabilities = sum(float(weight) * components[name] for name, weight in weights.items() if weight > 0)
    probabilities = np.clip(probabilities, 1e-9, None)
    return probabilities / probabilities.sum(axis=1, keepdims=True)


def _admission(metrics: dict[str, Any], config: dict[str, Any]) -> bool:
    return bool(
        metrics["balanced_accuracy"] >= config["min_balanced_accuracy"]
        and metrics["brier"] <= config["max_brier_score"]
        and metrics.get("brier_skill", -1) > config["min_brier_skill"]
    )


def _save_challenger(
    artifact: ModelArtifact,
    models_path: Path,
    frame: pd.DataFrame,
    test_labels: np.ndarray,
    prior: np.ndarray,
    promote: bool,
    config: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None]:
    version = artifact.model_version.replace(".", "_") + f"_{artifact.trained_at}"
    challengers = models_path / "challengers"
    challengers.mkdir(parents=True, exist_ok=True)
    challenger_path = challengers / f"{artifact.coin}_{artifact.horizon}_{version}.joblib"
    joblib.dump(artifact, challenger_path)
    challenger_path.with_suffix(".metrics.json").write_text(json.dumps(artifact.metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    if not promote or not _admission(artifact.metrics, config):
        return "CHALLENGER_REJECTED_OOS", str(challenger_path), None

    champion_path = models_path / f"{artifact.coin}_{artifact.horizon}.joblib"
    champion_comparison: dict[str, Any] | None = None
    if champion_path.exists():
        try:
            champion = joblib.load(champion_path)
            model = champion.pipeline.named_steps.get("model")
            if model.early_stopping is False and all(column in frame for column in champion.feature_columns):
                champion_probabilities = champion.predict_frame(frame.iloc[-len(test_labels):])
                champion_metrics = probability_metrics(champion_probabilities, test_labels, prior)
                champion_comparison = champion_metrics
                challenger_score = _selection_score(artifact.metrics)
                champion_score = _selection_score(champion_metrics)
                if challenger_score < champion_score or artifact.metrics["brier"] > champion_metrics["brier"] + 0.002:
                    return "CHALLENGER_WORSE_THAN_CHAMPION", str(challenger_path), champion_comparison
        except Exception:
            champion_comparison = None

    if champion_path.exists():
        archive = models_path / "champions"
        archive.mkdir(parents=True, exist_ok=True)
        shutil.copy2(champion_path, archive / f"{artifact.coin}_{artifact.horizon}_{int(time.time() * 1000)}.joblib")
    temporary = champion_path.with_suffix(".joblib.tmp")
    joblib.dump(artifact, temporary)
    os.replace(temporary, champion_path)
    champion_path.with_suffix(".metrics.json").write_text(json.dumps(artifact.metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return "CHAMPION_PROMOTED", str(challenger_path), champion_comparison


def audit_task(
    coin: str,
    horizon: str,
    horizon_steps: int,
    interval: str,
    asset_candles: pd.DataFrame,
    btc_candles: pd.DataFrame,
    models_path: Path,
    model_config: dict[str, Any],
    coverage: dict[str, int],
    promote: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    emit = progress or (lambda _message: None)
    full_frame = _prepare_frame(asset_candles, btc_candles)
    warm = full_frame[FEATURE_COLUMNS].notna().all(axis=1)
    positions = np.flatnonzero(warm.to_numpy())
    positions = positions[positions < len(full_frame) - horizon_steps]
    if len(positions) < int(model_config["min_training_samples"]):
        raise ValueError(f"Insufficient complete causal rows for {coin}/{horizon}: {len(positions)}")
    frame = full_frame.iloc[positions].reset_index(drop=True)
    static = _rule_components(full_frame, positions, interval)
    random_state = int(model_config["random_state"])

    train_stop = int(len(frame) * 0.60) - horizon_steps
    initial_features, removed_initial = _correlation_prune(frame, FEATURE_COLUMNS, train_stop)
    barrier_results: dict[str, Any] = {}
    labels_by_name: dict[str, np.ndarray] = {}
    for spec in DEFAULT_BARRIER_SPECS:
        labels_full = triple_barrier_labels(
            full_frame, horizon_steps, spec.atr_multiplier,
            volatility_multiplier=spec.volatility_multiplier,
            barrier_mode=spec.mode, use_structure=spec.use_structure,
        )
        labels = labels_full.iloc[positions].astype(int).to_numpy()
        labels_by_name[spec.name] = labels
        probabilities, validation_labels, prior = _walk_forward_ml(frame, labels, initial_features, horizon_steps, random_state)
        metrics = probability_metrics(probabilities, validation_labels, prior)
        balance = _class_balance(labels[:int(len(labels) * 0.75)])
        fractions = np.asarray(list(balance["fractions"].values()))
        imbalance_penalty = max(0.0, float(fractions.max() - 0.65)) + max(0.0, float(0.12 - fractions.min()))
        metrics["selection_score"] = _selection_score(metrics) - imbalance_penalty
        barrier_results[spec.name] = {"spec": asdict(spec), "class_balance_pretest": balance, "walk_forward": metrics}
        emit(f"{coin}/{horizon} barrier {spec.name}: brier={metrics['brier']:.5f} ece={metrics['ece']:.5f} ba={metrics['balanced_accuracy']:.4f}")
    selected_barrier = max(barrier_results, key=lambda name: barrier_results[name]["walk_forward"]["selection_score"])
    selected_spec = next(spec for spec in DEFAULT_BARRIER_SPECS if spec.name == selected_barrier)
    labels = labels_by_name[selected_barrier]

    selected_features, feature_audit = _select_features(frame, labels, horizon_steps, random_state)
    components, validation_labels, validation_prior = _walk_forward_components(
        frame, labels, selected_features, static, horizon_steps, random_state,
    )
    comparisons: dict[str, Any] = {}
    variants = {
        "rules_only": ["technical", "structure"],
        "ml_only": ["ml"],
        "technical_ml": ["technical", "ml"],
        "full_historically_evaluable": ["technical", "structure", "ml", "statistical", "btc"],
    }
    for name, component_names in variants.items():
        weights, metrics = _tune_weights(components, validation_labels, validation_prior, component_names)
        comparisons[name] = {"status": "EVALUATED_VALIDATION_ONLY", "weights": weights, "metrics": metrics}
    unavailable = {
        "technical_derivatives": f"not_evaluable: only {coverage.get('contexts', 0)} context snapshots",
        "technical_orderbook": f"not_evaluable: only {coverage.get('books', 0)} order-book aggregates",
        "technical_trade_flow": f"not_evaluable: only {coverage.get('trades', 0)} trade-flow aggregates",
    }
    comparisons.update({name: {"status": reason} for name, reason in unavailable.items()})
    selected_variant = max(variants, key=lambda name: comparisons[name]["metrics"]["selection_score"])
    selected_weights = comparisons[selected_variant]["weights"]

    full_names = variants["full_historically_evaluable"]
    ablation: dict[str, Any] = {}
    full_score = comparisons["full_historically_evaluable"]["metrics"]["selection_score"]
    for removed in full_names:
        names = [name for name in full_names if name != removed]
        weights, metrics = _tune_weights(components, validation_labels, validation_prior, names)
        ablation[removed] = {
            "status": "EVALUATED_VALIDATION_ONLY", "weights": weights, "metrics": metrics,
            "selection_score_delta_vs_full": float(metrics["selection_score"] - full_score),
        }
    for removed, rows in (("funding", coverage.get("contexts", 0)), ("oi", coverage.get("contexts", 0)),
                          ("orderbook", coverage.get("books", 0)), ("trade_flow", coverage.get("trades", 0))):
        ablation[removed] = {"status": "NOT_EVALUABLE_INSUFFICIENT_HISTORY", "rows": int(rows), "production_weight_recommendation": 0.0}

    final_components, test_labels, test_prior, pipeline, calibrator = _final_component_predictions(
        frame, labels, selected_features, static, horizon_steps, random_state,
    )
    final_probabilities = _combine(final_components, selected_weights)
    test_metrics = probability_metrics(final_probabilities, test_labels, test_prior)
    test_metrics["selection_score"] = _selection_score(test_metrics)
    ml_test_metrics = probability_metrics(final_components["ml"], test_labels, test_prior)
    ml_test_metrics["selection_score"] = _selection_score(ml_test_metrics)
    selection_end = int(len(labels) * 0.75)
    calibration_end = int(len(labels) * 0.90)
    test_start = calibration_end
    trained_at = int(time.time() * 1000)
    artifact_metrics = dict(ml_test_metrics)
    artifact_metrics.update({
        "model_version": MODEL_VERSION,
        "training_period": {"start_ms": int(frame["open_time"].iloc[0]), "end_ms": int(frame["open_time"].iloc[selection_end - horizon_steps - 1])},
        "calibration_period": {"start_ms": int(frame["open_time"].iloc[selection_end]), "end_ms": int(frame["open_time"].iloc[calibration_end - horizon_steps - 1])},
        "test_period": {"start_ms": int(frame["open_time"].iloc[test_start]), "end_ms": int(frame["open_time"].iloc[-1])},
        "test_samples": int(len(test_labels)),
        "feature_set": selected_features,
        "target_spec": {**asdict(selected_spec), "horizon_steps": horizon_steps},
        "ensemble_validation_variant": selected_variant,
        "ensemble_weights": selected_weights,
        "selected_ensemble_test_metrics": test_metrics,
    })
    artifact = ModelArtifact(
        coin=coin, horizon=horizon, feature_columns=selected_features, pipeline=pipeline,
        calibrator=calibrator, metrics=artifact_metrics, trained_at=trained_at,
        training_period=artifact_metrics["training_period"], model_version=MODEL_VERSION,
        target_spec=artifact_metrics["target_spec"], ensemble_weights=selected_weights,
        feature_importance=feature_audit["permutation_importance"],
    )
    status, challenger_path, champion_metrics = _save_challenger(
        artifact, models_path, frame, test_labels, test_prior, promote, model_config,
    )
    return {
        "coin": coin, "horizon": horizon, "source_interval": interval,
        "samples": int(len(labels)), "split": {
            "selection_end": selection_end, "calibration_end": calibration_end, "test_start": test_start,
            "purge_steps": horizon_steps,
        },
        "barrier_variants": barrier_results, "selected_barrier": selected_barrier,
        "class_balance_all": _class_balance(labels), "class_balance_test": _class_balance(test_labels),
        "feature_audit": feature_audit, "initially_removed_correlated": removed_initial,
        "validation_comparisons": comparisons, "ablation": ablation,
        "selected_variant": selected_variant, "selected_weights": selected_weights,
        "untouched_test_metrics": test_metrics, "ml_only_untouched_test_metrics": ml_test_metrics,
        "model_status": status, "challenger_path": challenger_path,
        "champion_same_holdout_metrics": champion_metrics, "trained_at": trained_at,
        "data_coverage": coverage,
    }


def run_quant_audit(
    config_path: str | Path = "config.yaml",
    tasks: list[tuple[str, str]] | None = None,
    promote: bool = True,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], Path]:
    config, _ = load_config(config_path)
    storage = SQLiteStorage(config["storage"]["sqlite_path"])
    repository = MarketRepository(storage)
    models_path = Path(config["storage"]["models_path"])
    chosen = tasks or [(coin, horizon) for coin in config["market"]["assets"] for horizon in config["models"]["horizons"]]
    report: dict[str, Any] = {
        "generated_at": int(time.time() * 1000), "model_version": MODEL_VERSION,
        "protocol": "purged chronological TRAIN/VALIDATION/CALIBRATION/untouched TEST; selection never sees TEST",
        "tasks": {},
    }
    try:
        for coin, horizon in chosen:
            settings = config["models"]["horizons"][horizon]
            interval = settings["source_interval"]
            asset = repository.candles(coin, interval, 5000)
            btc = repository.candles(config["market"]["context_asset"], interval, 5000)
            coverage = {
                "candles": len(asset), "btc_candles": len(btc),
                "contexts": len(repository.context_history(coin)),
                "books": int(storage.query("SELECT COUNT(*) AS n FROM book_aggregates WHERE coin=?", (coin,))[0]["n"]),
                "trades": int(storage.query("SELECT COUNT(*) AS n FROM trade_aggregates WHERE coin=?", (coin,))[0]["n"]),
            }
            key = f"{coin}/{horizon}"
            if progress:
                progress(f"START {key}: candles={len(asset)} btc={len(btc)}")
            result = audit_task(
                coin, horizon, int(settings["steps"]), interval, asset, btc, models_path,
                config["models"], coverage, promote, progress,
            )
            report["tasks"][key] = result
            ready = result["model_status"] == "CHAMPION_PROMOTED"
            metrics = result["ml_only_untouched_test_metrics"]
            repository.save_model_metrics(coin, horizon, result["trained_at"], ready, metrics)
            if progress:
                progress(f"DONE {key}: {result['model_status']} selected={result['selected_variant']} test_brier={result['untouched_test_metrics']['brier']:.5f}")
    finally:
        storage.close()
    models_path.mkdir(parents=True, exist_ok=True)
    output = models_path / f"quant_audit_{report['generated_at']}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report, output
