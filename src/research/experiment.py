from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .evaluation import ForecastRecord, evaluate_records
from .execution import ExecutionAssumptions
from .promotion import PromotionDecision, PromotionPolicy, decide_promotion
from .splits import SplitPlan


REQUIRED_BASELINES = ("no_trade", "momentum", "mean_reversion", "btc_beta", "current_production")
REQUIRED_ABLATIONS = (
    "without_news", "without_social", "without_onchain", "without_orderbook",
    "without_trade_flow", "without_derivatives", "without_market_truth",
)


@dataclass(frozen=True)
class ExperimentResult:
    status: str
    baselines: Mapping[str, dict]
    challenger: Mapping[str, object]
    ablations: Mapping[str, dict]
    promotion: PromotionDecision
    model_card: Mapping[str, object]


def _validate_test_records(records: Iterable[ForecastRecord], test_ids: set[str]) -> list[ForecastRecord]:
    result = list(records)
    ids = [item.opportunity_id for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate predictions in untouched test set")
    if set(ids) != test_ids:
        raise ValueError("evaluation does not exactly match the untouched test set")
    return result


def run_champion_challenger(
    *,
    split_plan: SplitPlan,
    costs: ExecutionAssumptions,
    baseline_records: Mapping[str, Iterable[ForecastRecord]],
    challenger_name: str,
    challenger_records: Iterable[ForecastRecord],
    ablation_records: Mapping[str, Iterable[ForecastRecord]],
    incumbent: str,
    policy: PromotionPolicy,
    dataset_snapshot_id: str,
) -> ExperimentResult:
    """Evaluate only the frozen test IDs after all selection/calibration is complete."""

    test_ids = set(split_plan.untouched_test_ids)
    baselines: dict[str, dict] = {}
    for name in REQUIRED_BASELINES:
        if name not in baseline_records:
            baselines[name] = {
                "status": "NOT_EVALUABLE", "sample_count": 0, "trade_count": 0,
                "net_expectancy_ci95": None, "precision_ci95": None,
                "reason": "baseline observations unavailable for this dataset",
            }
            continue
        baselines[name] = evaluate_records(
            _validate_test_records(baseline_records[name], test_ids), costs=costs
        )
    challenger = evaluate_records(
        _validate_test_records(challenger_records, test_ids), costs=costs
    )
    ablations: dict[str, dict] = {}
    for name in REQUIRED_ABLATIONS:
        if name not in ablation_records:
            ablations[name] = {
                "status": "NOT_EVALUABLE", "sample_count": 0, "trade_count": 0,
                "net_expectancy_ci95": None, "precision_ci95": None,
                "reason": "ablation observations unavailable for this dataset",
            }
        else:
            ablations[name] = evaluate_records(
                _validate_test_records(ablation_records[name], test_ids), costs=costs
            )
    for name, records in sorted(ablation_records.items()):
        if name not in ablations:
            ablations[name] = evaluate_records(
                _validate_test_records(records, test_ids), costs=costs
            )
    comparisons = dict(baselines)
    comparisons.update({f"ablation:{name}": metrics for name, metrics in ablations.items()})
    promotion = decide_promotion(
        challenger_name=challenger_name,
        challenger=challenger,
        baselines=comparisons,
        incumbent=incumbent,
        policy=policy,
    )
    missing_ablations = [
        name for name in REQUIRED_ABLATIONS if ablations[name]["status"] != "EVALUATED"
    ]
    if promotion.promote and missing_ablations:
        promotion = PromotionDecision(
            "RETAIN_CHAMPION", False, incumbent, challenger_name,
            ("required OOS ablations are not evaluable: " + ", ".join(missing_ablations),),
        )
    model_card = {
        "protocol_version": "phase4-3.0",
        "dataset_snapshot_id": dataset_snapshot_id,
        "challenger_name": challenger_name,
        "challenger": challenger,
        "baselines": baselines,
        "incumbent": incumbent,
        "cost_model": costs.to_dict(),
        "test_sample_count": challenger.get("sample_count", 0),
        "uncertainty_method": "deterministic time-block bootstrap 95% interval",
        "segments": challenger.get("segments", []),
        "regimes": challenger.get("regimes", []),
        "ablations": ablations,
        "rejected_variants": [
            {
                "name": name,
                "status": metrics["status"],
                "reason": metrics.get("reason", "non-positive OOS net expectancy after costs"),
            }
            for name, metrics in sorted(ablations.items())
            if metrics["status"] != "EVALUATED"
            or float(metrics.get("net_expectancy", 0.0)) <= 0
        ],
        "negative_results_preserved": True,
        "production_weight_changes": {},
        "limitations": [
            "observational backtest does not guarantee future performance",
            "promotion requires applicable baselines and independent OOS time blocks",
            "real execution is outside scope and remains disabled",
        ],
        "promotion": promotion.to_dict(),
    }
    status = "EVALUATED" if challenger.get("status") == "EVALUATED" else "NOT_EVALUABLE"
    return ExperimentResult(status, baselines, challenger, ablations, promotion, model_card)


def not_evaluable_model_card() -> dict:
    """Honest cold-start state when no real Phase 4 snapshot exists yet."""

    metrics = {
        "status": "NOT_EVALUABLE", "sample_count": 0, "trade_count": 0,
        "net_expectancy_ci95": None, "precision_ci95": None,
        "reason": "no reproducible point-in-time Phase 4 dataset snapshot is available",
    }
    return {
        "phase": 4,
        "protocol_version": "phase4-3.0",
        "status": "NOT_EVALUABLE",
        "target": "tp_before_stop",
        "horizons": ["5m", "15m", "1h", "4h", "24h"],
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
            "gates": "configured_per_experiment",
        },
        "dataset_snapshot_id": None,
        "baselines": {name: dict(metrics) for name in REQUIRED_BASELINES},
        "challenger": dict(metrics),
        "challenger_name": "segmented_two_stage_ranker",
        "ablations": {name: dict(metrics) for name in REQUIRED_ABLATIONS},
        "rejected_variants": [
            {"name": name, "status": "NOT_EVALUABLE", "reason": metrics["reason"]}
            for name in REQUIRED_ABLATIONS
        ],
        "promotion": {
            "status": "NOT_EVALUABLE", "promote": False,
            "incumbent": "existing SOL/HYPE production ensemble",
            "challenger": "two_stage_opportunity_ranker",
            "reasons": [metrics["reason"]],
        },
        "negative_results_preserved": True,
        "production_weight_changes": {},
        "real_execution_enabled": False,
        "real_performance_claim": False,
    }
