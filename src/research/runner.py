from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.data.storage import SQLiteStorage

from .dataset import CausalDatasetBuilder, DatasetSnapshot, stable_hash
from .evaluation import (
    CandidateGenerator, CrossSectionalRanker, ForecastRecord, OpportunityCandidate,
    evaluate_records,
)
from .execution import ExecutionAssumptions, TradeOpportunity, simulate_execution
from .experiment import (
    REQUIRED_ABLATIONS, ExperimentResult, not_evaluable_model_card,
    run_champion_challenger,
)
from .promotion import PromotionPolicy
from .registry import ExperimentRecord, ResearchRegistry
from .splits import PurgedWalkForwardSplitter, SplitPlan
from .targets import HORIZONS_MS
from .types import ForwardOutcome, ResearchRow


SEGMENTS = ("majors_l1", "liquid_alts", "derivatives_movers", "new_memecoin")
ABLATION_FAMILIES = {
    "without_news": "news",
    "without_social": "social",
    "without_onchain": "onchain",
    "without_orderbook": "orderbook",
    "without_trade_flow": "trade_flow",
    "without_derivatives": "derivatives",
    "without_market_truth": "market_truth",
}
BASELINE_FEATURES = {
    "momentum": ("momentum", "return_1", "closed_return"),
    "mean_reversion": ("momentum", "return_1", "closed_return"),
    "btc_beta": ("btc_beta", "asset_btc_beta_60"),
    "current_production": ("current_production_probability",),
}


class UntouchedTestAccessError(RuntimeError):
    pass


class UntouchedTestVault:
    """One-shot label access after every model and threshold is frozen."""

    def __init__(self, rows: Iterable[ResearchRow]) -> None:
        self._rows = {row.row_id: row for row in rows}
        self._opened = False

    def open(self, row_ids: Iterable[str], *, selection_frozen: bool) -> tuple[ResearchRow, ...]:
        if not selection_frozen:
            raise UntouchedTestAccessError(
                "untouched TEST cannot be accessed before selection is frozen"
            )
        if self._opened:
            raise UntouchedTestAccessError("untouched TEST may be accessed exactly once")
        requested = tuple(dict.fromkeys(row_ids))
        missing = [row_id for row_id in requested if row_id not in self._rows]
        if missing:
            raise KeyError(f"unknown TEST row IDs: {missing[:3]}")
        self._opened = True
        return tuple(self._rows[row_id] for row_id in requested)

    @property
    def opened(self) -> bool:
        return self._opened


@dataclass(frozen=True)
class RunnerConfig:
    folds: int
    test_fraction: float
    minimum_train_rows: int
    minimum_partition_rows: int
    embargo_ms: int
    top_k: int
    requested_notional_usd: float
    threshold_candidates: tuple[float, ...]
    regularization_candidates: tuple[float, ...]
    random_state: int
    minimum_predicted_net_return: float
    minimum_exit_feasibility_probability: float
    maximum_predicted_adverse_excursion: float
    maximum_predicted_volatility: float

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "RunnerConfig":
        gates = config["ranking_gates"]
        return cls(
            folds=int(config["walk_forward_folds"]),
            test_fraction=float(config["untouched_test_fraction"]),
            minimum_train_rows=int(config["minimum_train_rows"]),
            minimum_partition_rows=int(config["minimum_partition_rows"]),
            embargo_ms=int(float(config["embargo_seconds"]) * 1_000),
            top_k=int(config["top_k"]),
            requested_notional_usd=float(config["requested_notional_usd"]),
            threshold_candidates=tuple(float(value) for value in config["threshold_candidates"]),
            regularization_candidates=tuple(
                float(value) for value in config["regularization_candidates"]
            ),
            random_state=int(config.get("random_state", 42)),
            minimum_predicted_net_return=float(gates["minimum_predicted_net_return"]),
            minimum_exit_feasibility_probability=float(
                gates["minimum_exit_feasibility_probability"]
            ),
            maximum_predicted_adverse_excursion=float(
                gates["maximum_predicted_adverse_excursion"]
            ),
            maximum_predicted_volatility=float(gates["maximum_predicted_volatility"]),
        )


class ProbabilityModel(Protocol):
    threshold: float

    def predict(self, rows: Iterable[ResearchRow]) -> np.ndarray: ...


@dataclass
class LogisticProbabilityModel:
    horizon: str
    feature_names: tuple[str, ...]
    pipeline: Pipeline | None
    prior: float
    calibrator: IsotonicRegression | None
    threshold: float
    regularization: float
    excluded_family: str | None = None
    target: str = "tp_before_stop"

    def predict(self, rows: Iterable[ResearchRow]) -> np.ndarray:
        materialized = list(rows)
        if self.pipeline is None:
            raw = np.full(len(materialized), self.prior, dtype=float)
        else:
            raw = self.pipeline.predict_proba(
                _matrix(materialized, self.feature_names, self.excluded_family)
            )[:, 1]
        if self.calibrator is not None and len(raw):
            raw = self.calibrator.predict(raw)
        return np.clip(raw, 1e-6, 1 - 1e-6)


@dataclass
class ScoreProbabilityModel:
    feature_name: str
    sign: float
    center: float
    scale: float
    calibrator: IsotonicRegression | None
    threshold: float
    direct_probability: bool = False

    def predict(self, rows: Iterable[ResearchRow]) -> np.ndarray:
        raw_values = np.asarray([
            float(_feature_map(row).get(self.feature_name, self.center)) for row in rows
        ], dtype=float)
        if self.direct_probability:
            raw = np.clip(raw_values, 1e-6, 1 - 1e-6)
        else:
            z = self.sign * (raw_values - self.center) / max(self.scale, 1e-9)
            raw = 1.0 / (1.0 + np.exp(-np.clip(z, -20, 20)))
        if self.calibrator is not None and len(raw):
            raw = self.calibrator.predict(raw)
        return np.clip(raw, 1e-6, 1 - 1e-6)


@dataclass
class RegressionModel:
    target: str
    feature_names: tuple[str, ...]
    pipeline: Pipeline | None
    constant: float
    regularization: float
    excluded_family: str | None = None

    def predict(self, rows: Iterable[ResearchRow]) -> np.ndarray:
        materialized = list(rows)
        if self.pipeline is None:
            return np.full(len(materialized), self.constant, dtype=float)
        return np.asarray(self.pipeline.predict(
            _matrix(materialized, self.feature_names, self.excluded_family)
        ), dtype=float)


@dataclass
class PredictiveHeads:
    tp: ProbabilityModel
    expected_return: RegressionModel
    mae: RegressionModel
    mfe: RegressionModel
    realized_volatility: RegressionModel
    exit_feasibility: LogisticProbabilityModel


@dataclass(frozen=True)
class TaskArtifact:
    task: str
    segment: str
    horizon: str
    plan: SplitPlan
    challenger: PredictiveHeads
    training_prior: float
    baselines: Mapping[str, PredictiveHeads]
    ablations: Mapping[str, PredictiveHeads]
    split_sizes: Mapping[str, int]
    unavailable: Mapping[str, str]


@dataclass(frozen=True)
class EndToEndRun:
    experiment_id: str
    result: ExperimentResult | None
    model_card: Mapping[str, Any]
    split_plans: Mapping[str, Any]
    test_access_count: int


def _feature_family(source_kind: str) -> str:
    normalized = source_kind.lower().replace("-", "_")
    aliases = {
        "tradeflow": "trade_flow", "trade": "trade_flow", "book": "orderbook",
        "l2": "orderbook", "market": "market_truth", "canonical_price": "market_truth",
        "event": "news", "x": "social", "chain": "onchain",
    }
    return aliases.get(normalized, normalized)


def _feature_map(row: ResearchRow, excluded_family: str | None = None) -> dict[str, float]:
    values: dict[str, float] = {}
    for feature in row.features:
        if excluded_family is not None and _feature_family(feature.source_kind) == excluded_family:
            continue
        if isinstance(feature.value, bool):
            values[feature.name] = float(feature.value)
        elif isinstance(feature.value, (int, float)) and math.isfinite(float(feature.value)):
            values[feature.name] = float(feature.value)
    return values


def _matrix(
    rows: Iterable[ResearchRow],
    names: tuple[str, ...],
    excluded_family: str | None = None,
) -> np.ndarray:
    matrix = []
    for row in rows:
        values = _feature_map(row, excluded_family)
        matrix.append([values.get(name, np.nan) for name in names])
    return np.asarray(matrix, dtype=float)


def _outcome(row: ResearchRow, horizon: str) -> ForwardOutcome:
    matches = [item for item in row.outcomes if item.horizon == horizon]
    if len(matches) != 1:
        raise ValueError(f"row {row.row_id} must have exactly one {horizon} outcome")
    return matches[0]


def _labels(
    rows: Iterable[ResearchRow],
    horizon: str,
    target: str = "tp_before_stop",
) -> np.ndarray:
    labels = []
    for row in rows:
        outcome = _outcome(row, horizon)
        if target == "tp_before_stop":
            value = outcome.tp_before_stop
        elif target == "exit_feasible":
            value = outcome.exit_feasible
        else:
            raise ValueError(f"unknown binary target {target}")
        if value is None:
            raise ValueError(f"row {row.row_id} has no {target} target")
        labels.append(int(value))
    return np.asarray(labels, dtype=int)


def _complete_execution_outcome(outcome: ForwardOutcome) -> bool:
    values = (
        outcome.entry_price, outcome.exit_price,
        outcome.entry_liquidity_usd, outcome.exit_liquidity_usd,
    )
    complete = (
        outcome.tp_before_stop is not None
        and outcome.direction in (-1, 1)
        and all(value is not None and float(value) > 0 for value in values)
    )
    if not complete:
        return False
    assert outcome.entry_price is not None and outcome.exit_price is not None
    implied = outcome.direction * (outcome.exit_price / outcome.entry_price - 1.0)
    return abs(implied - outcome.gross_return) <= 1e-9


def _fit_raw_model(
    rows: list[ResearchRow],
    horizon: str,
    feature_names: tuple[str, ...],
    regularization: float,
    random_state: int,
    excluded_family: str | None = None,
    target: str = "tp_before_stop",
) -> tuple[Pipeline | None, float]:
    labels = _labels(rows, horizon, target)
    prior = float(np.mean(labels))
    if len(np.unique(labels)) < 2 or not feature_names:
        return None, prior
    pipeline = Pipeline([
        ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(
            C=regularization, solver="liblinear", random_state=random_state, max_iter=300,
        )),
    ])
    pipeline.fit(_matrix(rows, feature_names, excluded_family), labels)
    return pipeline, prior


def _raw_probability(
    pipeline: Pipeline | None,
    prior: float,
    rows: list[ResearchRow],
    feature_names: tuple[str, ...],
    excluded_family: str | None = None,
) -> np.ndarray:
    if pipeline is None:
        return np.full(len(rows), prior, dtype=float)
    return pipeline.predict_proba(_matrix(rows, feature_names, excluded_family))[:, 1]


def _fit_calibrator(probabilities: np.ndarray, labels: np.ndarray) -> IsotonicRegression | None:
    if len(probabilities) < 2 or len(np.unique(probabilities)) < 2 or len(np.unique(labels)) < 2:
        return None
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
    calibrator.fit(probabilities, labels)
    return calibrator


def _validate_execution_rows(rows: Iterable[ResearchRow], horizon: str) -> None:
    invalid = [row.row_id for row in rows if not _complete_execution_outcome(_outcome(row, horizon))]
    if invalid:
        raise ValueError(
            f"{horizon} execution evidence missing for {len(invalid)} rows; first={invalid[0]}"
        )


def execution_adjusted_return_target(
    row: ResearchRow,
    horizon: str,
    costs: ExecutionAssumptions,
    requested_notional_usd: float,
) -> float:
    """Recompute the realized target through the ledger; never use outcome.net_return."""

    outcome = _outcome(row, horizon)
    if not _complete_execution_outcome(outcome):
        raise ValueError(f"row {row.row_id} has incomplete execution evidence")
    assert outcome.entry_price is not None and outcome.exit_price is not None
    assert outcome.entry_liquidity_usd is not None and outcome.exit_liquidity_usd is not None
    result = simulate_execution(TradeOpportunity(
        opportunity_id=f"target:{row.row_id}|{horizon}",
        direction=outcome.direction,
        requested_notional_usd=requested_notional_usd,
        entry_price=float(outcome.entry_price),
        exit_price=float(outcome.exit_price),
        entry_liquidity_usd=float(outcome.entry_liquidity_usd),
        exit_liquidity_usd=float(outcome.exit_liquidity_usd),
    ), costs)
    return result.net_pnl_usd / requested_notional_usd


def _regression_targets(
    rows: Iterable[ResearchRow],
    horizon: str,
    target: str,
    costs: ExecutionAssumptions,
    requested_notional_usd: float,
) -> np.ndarray:
    values: list[float] = []
    for row in rows:
        outcome = _outcome(row, horizon)
        if target == "execution_adjusted_return":
            value = execution_adjusted_return_target(
                row, horizon, costs, requested_notional_usd
            )
        elif target == "mae":
            value = outcome.mae
        elif target == "mfe":
            value = outcome.mfe
        elif target == "realized_volatility":
            value = outcome.realized_volatility
        else:
            raise ValueError(f"unknown regression target {target}")
        if not math.isfinite(float(value)):
            raise ValueError(f"row {row.row_id} has non-finite {target} target")
        values.append(float(value))
    return np.asarray(values, dtype=float)


def _decision_feature(row: ResearchRow, names: tuple[str, ...], default: float = 0.0) -> float:
    values = _feature_map(row)
    for name in names:
        if name in values:
            return values[name]
    return default


def _records_for_predictions(
    rows: list[ResearchRow],
    horizon: str,
    probabilities: np.ndarray,
    expected_returns: np.ndarray,
    predicted_mae: np.ndarray,
    predicted_mfe: np.ndarray,
    predicted_volatility: np.ndarray,
    exit_probabilities: np.ndarray,
    *,
    threshold: float,
    config: RunnerConfig,
    force_no_trade: bool = False,
) -> list[ForecastRecord]:
    candidates: list[OpportunityCandidate] = []
    predictions = zip(
        rows, probabilities, expected_returns, predicted_mae, predicted_mfe,
        predicted_volatility, exit_probabilities, strict=True,
    )
    for row, probability, expected_return, mae, mfe, volatility, exit_probability in predictions:
        outcome = _outcome(row, horizon)
        decision_liquidity = _decision_feature(
            row, ("decision_liquidity_usd", "entry_liquidity_usd", "liquidity_usd")
        )
        risk_eligible = bool(_decision_feature(row, ("risk_eligible",), 0.0))
        candidates.append(OpportunityCandidate(
            opportunity_id=f"{row.row_id}|{horizon}", row_id=row.row_id, horizon=horizon,
            decision_at_ms=row.decision_at_ms, instrument_id=row.instrument_id,
            segment=row.universe_bucket, regime=row.regime,
            tp_probability=float(probability),
            expected_net_return=float(expected_return),
            predicted_mae=min(float(mae), 0.0), predicted_mfe=max(float(mfe), 0.0),
            predicted_volatility=max(float(volatility), 0.0),
            exit_feasibility_probability=float(np.clip(exit_probability, 0.0, 1.0)),
            liquidity_usd=decision_liquidity, risk_eligible=risk_eligible,
            signals={"tp_probability": float(probability)},
        ))
    if force_no_trade:
        selected_ids: set[str] = set()
    else:
        generated = CandidateGenerator(
            minimum_probability=threshold,
            task_thresholds={f"{rows[0].universe_bucket}:{horizon}": threshold},
            minimum_expected_return=config.minimum_predicted_net_return,
            minimum_exit_feasibility_probability=(
                config.minimum_exit_feasibility_probability
            ),
            maximum_predicted_adverse_excursion=(
                config.maximum_predicted_adverse_excursion
            ),
            maximum_predicted_volatility=config.maximum_predicted_volatility,
        ).generate(candidates) if rows else []
        selected_ids = {
            item.opportunity_id for item in CrossSectionalRanker(top_k=config.top_k).rank(generated)
        }
    ordered_times = sorted({row.decision_at_ms for row in rows})
    time_blocks = {
        value: f"test-block-{min(index * 3 // max(len(ordered_times), 1), 2)}"
        for index, value in enumerate(ordered_times)
    }
    records: list[ForecastRecord] = []
    predictions = zip(
        rows, probabilities, expected_returns, predicted_mae, predicted_mfe,
        predicted_volatility, exit_probabilities, strict=True,
    )
    for row, probability, expected_return, mae, mfe, volatility, exit_probability in predictions:
        outcome = _outcome(row, horizon)
        assert outcome.entry_price is not None and outcome.exit_price is not None
        assert outcome.entry_liquidity_usd is not None and outcome.exit_liquidity_usd is not None
        opportunity_id = f"{row.row_id}|{horizon}"
        records.append(ForecastRecord(
            opportunity_id=opportunity_id, row_id=row.row_id, horizon=horizon,
            time_block=time_blocks[row.decision_at_ms], segment=row.universe_bucket,
            regime=row.regime, tp_probability=float(probability),
            tp_before_stop_target=bool(outcome.tp_before_stop),
            selected=opportunity_id in selected_ids, direction=outcome.direction,
            requested_notional_usd=config.requested_notional_usd,
            entry_price=float(outcome.entry_price), exit_price=float(outcome.exit_price),
            entry_liquidity_usd=float(outcome.entry_liquidity_usd),
            exit_liquidity_usd=float(outcome.exit_liquidity_usd),
            expected_net_return=float(expected_return),
            predicted_mae=min(float(mae), 0.0), predicted_mfe=max(float(mfe), 0.0),
            predicted_volatility=max(float(volatility), 0.0),
            exit_feasibility_probability=float(np.clip(exit_probability, 0.0, 1.0)),
            realized_mae=float(outcome.mae), realized_mfe=float(outcome.mfe),
            realized_volatility=float(outcome.realized_volatility),
            exit_feasible_target=bool(outcome.exit_feasible),
        ))
    return records


def _choose_threshold(
    rows: list[ResearchRow],
    horizon: str,
    probabilities: np.ndarray,
    config: RunnerConfig,
) -> float:
    labels = _labels(rows, horizon)
    best: tuple[float, float, float] | None = None
    selected_threshold = 1.000001
    for threshold in config.threshold_candidates:
        predicted = probabilities >= threshold
        true_positive = int(np.sum(predicted & (labels == 1)))
        precision = true_positive / max(int(np.sum(predicted)), 1)
        recall = true_positive / max(int(np.sum(labels == 1)), 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-12)
        score = (f1, precision, -threshold)
        if best is None or score > best:
            best = score
            selected_threshold = threshold
    assert best is not None
    if best[0] <= 0:
        return 1.000001
    return selected_threshold


def _fit_logistic_variant(
    *,
    rows_by_id: Mapping[str, ResearchRow],
    plan: SplitPlan,
    horizon: str,
    config: RunnerConfig,
    costs: ExecutionAssumptions,
    excluded_family: str | None = None,
    target: str = "tp_before_stop",
    forced_feature_names: tuple[str, ...] | None = None,
) -> LogisticProbabilityModel:
    validation_scores: dict[float, list[float]] = {
        value: [] for value in config.regularization_candidates
    }
    for fold in plan.folds:
        train = [rows_by_id[row_id] for row_id in fold.train_ids]
        validation = [rows_by_id[row_id] for row_id in fold.validation_ids]
        names = forced_feature_names or tuple(sorted({
            name for row in train for name in _feature_map(row, excluded_family)
            if name not in {"risk_eligible", "decision_liquidity_usd", "entry_liquidity_usd"}
        }))
        labels = _labels(validation, horizon, target)
        for regularization in config.regularization_candidates:
            pipeline, prior = _fit_raw_model(
                train, horizon, names, regularization, config.random_state + fold.fold,
                excluded_family, target,
            )
            probability = _raw_probability(
                pipeline, prior, validation, names, excluded_family
            )
            validation_scores[regularization].append(
                float(np.mean((probability - labels) ** 2))
            )
    regularization = min(
        config.regularization_candidates,
        key=lambda value: (sum(validation_scores[value]) / len(validation_scores[value]), value),
    )
    final = plan.folds[-1]
    train = [rows_by_id[row_id] for row_id in (*final.train_ids, *final.validation_ids)]
    calibration = [rows_by_id[row_id] for row_id in final.calibration_ids]
    middle = max(1, len(calibration) // 2)
    calibration_fit = calibration[:middle]
    calibration_select = calibration[middle:]
    if not calibration_select:
        raise ValueError("calibration partition cannot tune threshold after calibration fit")
    if target == "tp_before_stop" and len(np.unique(_labels(train, horizon, target))) < 2:
        raise ValueError(f"{horizon} training target has fewer than two classes")
    if target == "tp_before_stop" and len(
        np.unique(_labels(calibration_fit, horizon, target))
    ) < 2:
        raise ValueError(f"{horizon} calibration target has fewer than two classes")
    names = forced_feature_names or tuple(sorted({
        name for row in train for name in _feature_map(row, excluded_family)
        if name not in {"risk_eligible", "decision_liquidity_usd", "entry_liquidity_usd"}
    }))
    pipeline, prior = _fit_raw_model(
        train, horizon, names, regularization, config.random_state + 10_000,
        excluded_family, target,
    )
    fit_raw = _raw_probability(
        pipeline, prior, calibration_fit, names, excluded_family
    )
    calibrator = _fit_calibrator(fit_raw, _labels(calibration_fit, horizon, target))
    select_raw = _raw_probability(
        pipeline, prior, calibration_select, names, excluded_family
    )
    select_probability = calibrator.predict(select_raw) if calibrator is not None else select_raw
    threshold = _choose_threshold(
        calibration_select, horizon, np.clip(select_probability, 1e-6, 1 - 1e-6),
        config,
    ) if target == "tp_before_stop" else 0.5
    return LogisticProbabilityModel(
        horizon, names, pipeline, prior, calibrator, threshold, regularization,
        excluded_family, target,
    )


def _fit_raw_regression(
    rows: list[ResearchRow],
    horizon: str,
    feature_names: tuple[str, ...],
    regularization: float,
    target: str,
    costs: ExecutionAssumptions,
    config: RunnerConfig,
    excluded_family: str | None,
) -> tuple[Pipeline | None, float]:
    values = _regression_targets(
        rows, horizon, target, costs, config.requested_notional_usd
    )
    constant = float(np.mean(values))
    if not feature_names or float(np.std(values)) <= 1e-12:
        return None, constant
    pipeline = Pipeline([
        ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("scale", StandardScaler()),
        ("model", Ridge(alpha=regularization)),
    ])
    pipeline.fit(_matrix(rows, feature_names, excluded_family), values)
    return pipeline, constant


def _fit_regression_head(
    *,
    rows_by_id: Mapping[str, ResearchRow],
    plan: SplitPlan,
    horizon: str,
    target: str,
    config: RunnerConfig,
    costs: ExecutionAssumptions,
    excluded_family: str | None = None,
    forced_feature_names: tuple[str, ...] | None = None,
) -> RegressionModel:
    validation_scores: dict[float, list[float]] = {
        value: [] for value in config.regularization_candidates
    }
    for fold in plan.folds:
        train = [rows_by_id[row_id] for row_id in fold.train_ids]
        validation = [rows_by_id[row_id] for row_id in fold.validation_ids]
        names = forced_feature_names or tuple(sorted({
            name for row in train for name in _feature_map(row, excluded_family)
            if name not in {"risk_eligible", "decision_liquidity_usd", "entry_liquidity_usd"}
        }))
        actual = _regression_targets(
            validation, horizon, target, costs, config.requested_notional_usd
        )
        for regularization in config.regularization_candidates:
            pipeline, constant = _fit_raw_regression(
                train, horizon, names, regularization, target, costs, config,
                excluded_family,
            )
            predicted = np.full(len(validation), constant) if pipeline is None else np.asarray(
                pipeline.predict(_matrix(validation, names, excluded_family)), dtype=float
            )
            validation_scores[regularization].append(float(np.mean((predicted - actual) ** 2)))
    regularization = min(
        config.regularization_candidates,
        key=lambda value: (sum(validation_scores[value]) / len(validation_scores[value]), value),
    )
    final = plan.folds[-1]
    train = [rows_by_id[row_id] for row_id in (*final.train_ids, *final.validation_ids)]
    names = forced_feature_names or tuple(sorted({
        name for row in train for name in _feature_map(row, excluded_family)
        if name not in {"risk_eligible", "decision_liquidity_usd", "entry_liquidity_usd"}
    }))
    pipeline, constant = _fit_raw_regression(
        train, horizon, names, regularization, target, costs, config, excluded_family
    )
    return RegressionModel(
        target, names, pipeline, constant, regularization, excluded_family
    )


def _fit_predictive_heads(
    *,
    rows_by_id: Mapping[str, ResearchRow],
    plan: SplitPlan,
    horizon: str,
    config: RunnerConfig,
    costs: ExecutionAssumptions,
    excluded_family: str | None = None,
    forced_feature_names: tuple[str, ...] | None = None,
    tp_model: ProbabilityModel | None = None,
) -> PredictiveHeads:
    tp = tp_model or _fit_logistic_variant(
        rows_by_id=rows_by_id, plan=plan, horizon=horizon, config=config, costs=costs,
        excluded_family=excluded_family, forced_feature_names=forced_feature_names,
    )
    regressions = {
        target: _fit_regression_head(
            rows_by_id=rows_by_id, plan=plan, horizon=horizon, target=target,
            config=config, costs=costs, excluded_family=excluded_family,
            forced_feature_names=forced_feature_names,
        )
        for target in ("execution_adjusted_return", "mae", "mfe", "realized_volatility")
    }
    exit_model = _fit_logistic_variant(
        rows_by_id=rows_by_id, plan=plan, horizon=horizon, config=config, costs=costs,
        excluded_family=excluded_family, target="exit_feasible",
        forced_feature_names=forced_feature_names,
    )
    return PredictiveHeads(
        tp=tp,
        expected_return=regressions["execution_adjusted_return"],
        mae=regressions["mae"],
        mfe=regressions["mfe"],
        realized_volatility=regressions["realized_volatility"],
        exit_feasibility=exit_model,
    )


def _baseline_feature(rows: Iterable[ResearchRow], baseline: str) -> str | None:
    available = {name for row in rows for name in _feature_map(row)}
    return next((name for name in BASELINE_FEATURES[baseline] if name in available), None)


def _fit_score_baseline(
    *,
    rows_by_id: Mapping[str, ResearchRow],
    plan: SplitPlan,
    horizon: str,
    baseline: str,
    config: RunnerConfig,
    costs: ExecutionAssumptions,
) -> ScoreProbabilityModel | None:
    final = plan.folds[-1]
    train = [rows_by_id[row_id] for row_id in (*final.train_ids, *final.validation_ids)]
    calibration = [rows_by_id[row_id] for row_id in final.calibration_ids]
    feature_name = _baseline_feature(train, baseline)
    if feature_name is None:
        return None
    values = np.asarray([_feature_map(row).get(feature_name, np.nan) for row in train], dtype=float)
    finite = values[np.isfinite(values)]
    if not len(finite):
        return None
    center = float(np.median(finite))
    scale = float(np.std(finite)) or 1.0
    direct = baseline == "current_production"
    sign = -1.0 if baseline == "mean_reversion" else 1.0
    model = ScoreProbabilityModel(feature_name, sign, center, scale, None, 0.5, direct)
    middle = max(1, len(calibration) // 2)
    calibration_fit, calibration_select = calibration[:middle], calibration[middle:]
    if not calibration_select:
        return None
    raw = model.predict(calibration_fit)
    model.calibrator = _fit_calibrator(raw, _labels(calibration_fit, horizon))
    probability = model.predict(calibration_select)
    model.threshold = _choose_threshold(
        calibration_select, horizon, probability, config
    )
    return model


def _task_rows(snapshot: DatasetSnapshot, segment: str, horizon: str) -> list[ResearchRow]:
    result = []
    for row in snapshot.rows:
        if row.universe_bucket != segment:
            continue
        matches = tuple(item for item in row.outcomes if item.horizon == horizon)
        if len(matches) == 1:
            if horizon not in HORIZONS_MS or matches[0].horizon_ms != HORIZONS_MS[horizon]:
                raise ValueError(f"{horizon} target duration does not match its declared horizon")
            if matches[0].label_end_at_ms < row.decision_at_ms + HORIZONS_MS[horizon]:
                raise ValueError(f"{horizon} label ends before its forward horizon")
            result.append(replace(row, outcomes=matches))
    return result


def _fit_task(
    snapshot: DatasetSnapshot,
    segment: str,
    horizon: str,
    config: RunnerConfig,
    costs: ExecutionAssumptions,
) -> TaskArtifact:
    rows = _task_rows(snapshot, segment, horizon)
    plan = PurgedWalkForwardSplitter(
        folds=config.folds, test_fraction=config.test_fraction,
        minimum_train_rows=config.minimum_train_rows,
        minimum_partition_rows=config.minimum_partition_rows,
        embargo_ms=config.embargo_ms,
    ).split(rows)
    rows_by_id = {row.row_id: row for row in rows if row.row_id not in plan.untouched_test_ids}
    development_ids = {
        row_id for fold in plan.folds
        for row_id in (*fold.train_ids, *fold.validation_ids, *fold.calibration_ids)
    }
    development = [rows_by_id[row_id] for row_id in development_ids]
    _validate_execution_rows(development, horizon)
    challenger = _fit_predictive_heads(
        rows_by_id=rows_by_id, plan=plan, horizon=horizon, config=config, costs=costs
    )
    baselines: dict[str, PredictiveHeads] = {}
    unavailable: dict[str, str] = {}
    for baseline in BASELINE_FEATURES:
        tp_model = _fit_score_baseline(
            rows_by_id=rows_by_id, plan=plan, horizon=horizon, baseline=baseline,
            config=config, costs=costs,
        )
        if tp_model is None:
            unavailable[baseline] = "required causal feature unavailable"
        else:
            baselines[baseline] = _fit_predictive_heads(
                rows_by_id=rows_by_id, plan=plan, horizon=horizon, config=config,
                costs=costs, forced_feature_names=(tp_model.feature_name,),
                tp_model=tp_model,
            )
    ablations: dict[str, PredictiveHeads] = {}
    source_families = {
        _feature_family(feature.source_kind) for row in development for feature in row.features
    }
    for name, family in ABLATION_FAMILIES.items():
        if family not in source_families:
            unavailable[name] = f"{family} feature family unavailable"
            continue
        model = _fit_predictive_heads(
            rows_by_id=rows_by_id, plan=plan, horizon=horizon, config=config,
            costs=costs, excluded_family=family,
        )
        if not model.tp.feature_names:
            unavailable[name] = "ablation leaves no usable features"
        else:
            ablations[name] = model
    final = plan.folds[-1]
    return TaskArtifact(
        task=f"{segment}:{horizon}", segment=segment, horizon=horizon, plan=plan,
        challenger=challenger,
        training_prior=float(np.mean(_labels(
            [rows_by_id[row_id] for row_id in final.train_ids], horizon
        ))),
        baselines=baselines, ablations=ablations,
        split_sizes={
            "train": len(final.train_ids), "validation": len(final.validation_ids),
            "calibration": len(final.calibration_ids), "test": len(final.test_ids),
        },
        unavailable=unavailable,
    )


def _head_predictions(
    heads: PredictiveHeads,
    rows: list[ResearchRow],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    return (
        heads.tp.predict(rows),
        heads.expected_return.predict(rows),
        heads.mae.predict(rows),
        heads.mfe.predict(rows),
        heads.realized_volatility.predict(rows),
        heads.exit_feasibility.predict(rows),
    )


def _heads_metadata(heads: PredictiveHeads) -> dict[str, Any]:
    tp_features = list(getattr(heads.tp, "feature_names", ()))
    if not tp_features and hasattr(heads.tp, "feature_name"):
        tp_features = [str(getattr(heads.tp, "feature_name"))]
    return {
        "tp_before_stop": {
            "feature_names": tp_features,
            "threshold": heads.tp.threshold,
            "calibrated": getattr(heads.tp, "calibrator", None) is not None,
        },
        "execution_adjusted_return": {
            "feature_names": list(heads.expected_return.feature_names),
            "regularization": heads.expected_return.regularization,
            "units": "return_fraction_on_requested_notional",
        },
        "mae": {"feature_names": list(heads.mae.feature_names)},
        "mfe": {"feature_names": list(heads.mfe.feature_names)},
        "realized_volatility": {
            "feature_names": list(heads.realized_volatility.feature_names)
        },
        "exit_feasibility": {
            "feature_names": list(heads.exit_feasibility.feature_names),
            "calibrated": heads.exit_feasibility.calibrator is not None,
        },
    }


def _predict_task_records(
    artifact: TaskArtifact,
    test_rows: list[ResearchRow],
    config: RunnerConfig,
) -> tuple[list[ForecastRecord], dict[str, list[ForecastRecord]], dict[str, list[ForecastRecord]]]:
    rows = [row for row in test_rows if row.row_id in artifact.plan.untouched_test_ids]
    rows = [replace(row, outcomes=tuple(
        item for item in row.outcomes if item.horizon == artifact.horizon
    )) for row in rows]
    _validate_execution_rows(rows, artifact.horizon)
    challenger_predictions = _head_predictions(artifact.challenger, rows)
    challenger = _records_for_predictions(
        rows, artifact.horizon, *challenger_predictions,
        threshold=artifact.challenger.tp.threshold, config=config,
    )
    baselines: dict[str, list[ForecastRecord]] = {}
    baselines["no_trade"] = _records_for_predictions(
        rows, artifact.horizon,
        np.full(len(rows), artifact.training_prior), np.zeros(len(rows)),
        np.zeros(len(rows)), np.zeros(len(rows)), np.zeros(len(rows)), np.ones(len(rows)),
        threshold=1.000001, config=config, force_no_trade=True,
    )
    for name, heads in artifact.baselines.items():
        baselines[name] = _records_for_predictions(
            rows, artifact.horizon, *_head_predictions(heads, rows),
            threshold=heads.tp.threshold, config=config,
        )
    ablations = {
        name: _records_for_predictions(
            rows, artifact.horizon, *_head_predictions(heads, rows),
            threshold=heads.tp.threshold, config=config,
        )
        for name, heads in artifact.ablations.items()
    }
    return challenger, baselines, ablations


def run_snapshot_experiment(
    *,
    snapshot: DatasetSnapshot,
    snapshot_path: str | Path,
    config: Mapping[str, Any],
    storage: SQLiteStorage,
    created_at_ms: int | None = None,
) -> EndToEndRun:
    """Fit, calibrate, freeze, open TEST once, backtest, evaluate and register."""

    for row in snapshot.rows:
        CausalDatasetBuilder.validate_row(row)
    research = config["research"]
    runner_config = RunnerConfig.from_mapping(research)
    costs = ExecutionAssumptions.from_mapping(research["execution_costs"], require_complete=True)
    promotion_config = research["promotion"]
    policy = PromotionPolicy(
        minimum_test_samples=int(promotion_config["minimum_test_samples"]),
        minimum_trades=int(promotion_config["minimum_trades"]),
        minimum_time_blocks=int(promotion_config["minimum_time_blocks"]),
        maximum_drawdown=float(promotion_config["maximum_drawdown"]),
        maximum_ece=float(promotion_config["maximum_ece"]),
        require_all_blocks_positive=bool(promotion_config["require_all_blocks_positive"]),
    )
    registry = ResearchRegistry(storage)
    now = int(created_at_ms if created_at_ms is not None else time.time() * 1_000)
    registry.register_snapshot(snapshot, snapshot_path, created_at_ms=now)

    artifacts: list[TaskArtifact] = []
    unavailable_tasks: dict[str, str] = {}
    requested_horizons = tuple(str(value) for value in research["horizons"])
    for segment in SEGMENTS:
        for horizon in requested_horizons:
            task = f"{segment}:{horizon}"
            try:
                artifacts.append(_fit_task(snapshot, segment, horizon, runner_config, costs))
            except (ValueError, KeyError) as exc:
                unavailable_tasks[task] = str(exc)

    split_plans = {artifact.task: artifact.plan.to_dict() for artifact in artifacts}
    split_summary = {
        artifact.task: {**artifact.split_sizes, "unavailable": dict(artifact.unavailable)}
        for artifact in artifacts
    }
    task_models = {
        artifact.task: {
            "challenger": _heads_metadata(artifact.challenger),
            "baselines": {
                name: _heads_metadata(heads)
                for name, heads in sorted(artifact.baselines.items())
            },
            "ablations": {
                name: _heads_metadata(heads)
                for name, heads in sorted(artifact.ablations.items())
            },
        }
        for artifact in artifacts
    }
    experiment_material = {
        "protocol": "phase4-3.0", "snapshot_id": snapshot.snapshot_id,
        "config_hash": stable_hash(research), "costs": costs.to_dict(),
    }
    experiment_id = stable_hash(experiment_material)
    if not artifacts:
        card = not_evaluable_model_card()
        card = {
            **card, "protocol_version": "phase4-3.0", "dataset_snapshot_id": snapshot.snapshot_id,
            "dataset_code_version": snapshot.code_version,
            "dataset_config_hash": snapshot.config_hash,
            "dataset_universe_definition": dict(snapshot.universe_definition),
            "experiment_id": experiment_id, "unavailable_tasks": unavailable_tasks,
            "task_models": task_models,
        }
        registry.register_experiment(ExperimentRecord(
            experiment_id, snapshot.snapshot_id, now, "NOT_EVALUABLE", split_plans,
            costs.to_dict(), card["baselines"], card["challenger"], card,
            card["promotion"],
        ))
        return EndToEndRun(experiment_id, None, card, split_plans, 0)

    vault = UntouchedTestVault(snapshot.rows)
    all_test_row_ids = tuple(dict.fromkeys(
        row_id for artifact in artifacts for row_id in artifact.plan.untouched_test_ids
    ))
    test_rows = list(vault.open(all_test_row_ids, selection_frozen=True))
    challenger_records: list[ForecastRecord] = []
    baseline_parts: dict[str, list[list[ForecastRecord]]] = {
        name: [] for name in ("no_trade", *BASELINE_FEATURES)
    }
    ablation_parts: dict[str, list[list[ForecastRecord]]] = {
        name: [] for name in REQUIRED_ABLATIONS
    }
    evaluated_tasks: list[TaskArtifact] = []
    for artifact in artifacts:
        try:
            challenger, baselines, ablations = _predict_task_records(
                artifact, test_rows, runner_config
            )
        except ValueError as exc:
            unavailable_tasks[artifact.task] = str(exc)
            continue
        evaluated_tasks.append(artifact)
        challenger_records.extend(challenger)
        for name in baseline_parts:
            if name in baselines:
                baseline_parts[name].append(baselines[name])
        for name in ablation_parts:
            if name in ablations:
                ablation_parts[name].append(ablations[name])
    if not evaluated_tasks:
        card = not_evaluable_model_card()
        card = {
            **card, "protocol_version": "phase4-3.0", "dataset_snapshot_id": snapshot.snapshot_id,
            "dataset_code_version": snapshot.code_version,
            "dataset_config_hash": snapshot.config_hash,
            "dataset_universe_definition": dict(snapshot.universe_definition),
            "experiment_id": experiment_id, "unavailable_tasks": unavailable_tasks,
            "task_models": task_models,
            "test_access_count": 1,
        }
        registry.register_experiment(ExperimentRecord(
            experiment_id, snapshot.snapshot_id, now, "NOT_EVALUABLE", split_plans,
            costs.to_dict(), card["baselines"], card["challenger"], card,
            card["promotion"],
        ))
        return EndToEndRun(experiment_id, None, card, split_plans, 1)

    task_count = len(evaluated_tasks)
    baseline_records = {
        name: [record for part in parts for record in part]
        for name, parts in baseline_parts.items() if len(parts) == task_count
    }
    ablation_records = {
        name: [record for part in parts for record in part]
        for name, parts in ablation_parts.items() if len(parts) == task_count
    }
    opportunity_ids = tuple(record.opportunity_id for record in challenger_records)
    combined_plan = SplitPlan((), runner_config.embargo_ms, True, opportunity_ids)
    result = run_champion_challenger(
        split_plan=combined_plan, costs=costs, baseline_records=baseline_records,
        challenger_name="segmented_two_stage_ranker", challenger_records=challenger_records,
        ablation_records=ablation_records,
        incumbent="existing SOL/HYPE production ensemble", policy=policy,
        dataset_snapshot_id=snapshot.snapshot_id,
    )
    task_metrics = {}
    for artifact in evaluated_tasks:
        task_ids = {
            f"{row_id}|{artifact.horizon}" for row_id in artifact.plan.untouched_test_ids
        }
        task_metrics[artifact.task] = evaluate_records(
            [record for record in challenger_records if record.opportunity_id in task_ids],
            costs=costs, top_k=runner_config.top_k,
        )
    card = {
        **result.model_card,
        "phase": 4,
        "protocol_version": "phase4-3.0",
        "experiment_id": experiment_id,
        "dataset_code_version": snapshot.code_version,
        "dataset_config_hash": snapshot.config_hash,
        "dataset_universe_definition": dict(snapshot.universe_definition),
        "status": result.status,
        "baselines": result.baselines,
        "challenger": result.challenger,
        "task_metrics": task_metrics,
        "split_sizes": split_summary,
        "task_models": task_models,
        "predictive_targets": [
            "tp_before_stop", "execution_adjusted_return", "mae", "mfe",
            "realized_volatility", "exit_feasibility",
        ],
        "ranking": {
            "primary_key": "predicted_execution_adjusted_return",
            "tie_breakers": [
                "calibrated_tp_probability", "exit_feasibility_probability",
                "predicted_mfe", "decision_liquidity_usd",
            ],
            "gates": dict(research["ranking_gates"]),
        },
        "unavailable_tasks": unavailable_tasks,
        "test_access_count": 1,
        "real_execution_enabled": False,
        "real_performance_claim": False,
    }
    registry.register_experiment(ExperimentRecord(
        experiment_id, snapshot.snapshot_id, now, result.status, split_plans,
        costs.to_dict(), result.baselines, result.challenger, card,
        result.promotion.to_dict(),
    ))
    return EndToEndRun(experiment_id, result, card, split_plans, 1)


def write_model_card(card: Mapping[str, Any], path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(card, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return destination
