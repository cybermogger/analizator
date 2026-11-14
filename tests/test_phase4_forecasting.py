from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
import numpy as np

from src.data.storage import SCHEMA_VERSION, SQLiteStorage
from src.research.dataset import CausalDatasetBuilder, DatasetSnapshot, LeakageError
from src.research.evaluation import (
    CandidateGenerator, CrossSectionalRanker, ForecastRecord, OpportunityCandidate,
    evaluate_records,
)
from src.research.execution import ExecutionAssumptions, TradeOpportunity, simulate_execution
from src.research.experiment import (
    REQUIRED_ABLATIONS, REQUIRED_BASELINES, not_evaluable_model_card,
    run_champion_challenger,
)
from src.research.promotion import PromotionPolicy, decide_promotion
from src.research.registry import ExperimentRecord, ResearchRegistry
from src.research.runner import (
    RunnerConfig, UntouchedTestAccessError, UntouchedTestVault,
    _fit_predictive_heads, _task_rows,
)
from src.research.splits import PurgedWalkForwardSplitter, SplitPlan
from src.research.targets import PricePoint, forward_outcomes
from src.research.types import FeatureObservation, ForwardOutcome, ResearchRow


def outcome(decision: int, end: int | None = None) -> ForwardOutcome:
    return ForwardOutcome(
        horizon="5m", horizon_ms=500, label_end_at_ms=end or decision + 500,
        gross_return=0.01, net_return=0.008, tp_before_stop=True,
        mae=-0.002, mfe=0.012, realized_volatility=0.003, exit_feasible=True,
        direction=1, entry_price=100.0, exit_price=101.0,
        entry_liquidity_usd=100_000.0, exit_liquidity_usd=100_000.0,
    )


def row(index: int, feature: FeatureObservation | None = None, end: int | None = None) -> ResearchRow:
    decision = index * 1_000
    feature = feature or FeatureObservation(
        name="closed_return", value=0.01, available_at_ms=decision - 1,
        source_kind="candle", lineage_id=f"lineage-{index}",
        candle_close_at_ms=decision - 1, candle_closed=True,
    )
    return ResearchRow(
        row_id=f"r{index}", instrument_id="coinbase:spot:SOL-USD",
        canonical_asset_id="native:solana:SOL", universe_bucket="majors_l1",
        regime="normal", decision_at_ms=decision,
        universe_valid_from_ms=0, universe_valid_to_ms=None,
        instrument_active_at_decision=True, features=(feature,),
        outcomes=(outcome(decision, end),),
    )


def builder() -> CausalDatasetBuilder:
    return CausalDatasetBuilder(
        code_version="test-commit", config={"costs": {"fee": 4}},
        feature_schema={"closed_return": "float64"},
        universe_definition={"method": "point_in_time", "max_size": 150},
    )


def forecast(
    identifier: str,
    *,
    probability: float = 0.7,
    target: bool = True,
    selected: bool = True,
    gross_return: float = 0.01,
    block: str = "block",
    horizon: str = "5m",
) -> ForecastRecord:
    return ForecastRecord(
        opportunity_id=identifier, row_id=identifier.split("|")[0], horizon=horizon,
        time_block=block, segment="majors_l1", regime="normal",
        tp_probability=probability, tp_before_stop_target=target, selected=selected,
        direction=1, requested_notional_usd=1_000, entry_price=100,
        exit_price=100 * (1 + gross_return), entry_liquidity_usd=1_000_000,
        exit_liquidity_usd=1_000_000,
    )


def test_checked_in_cold_start_model_card_is_reproducible_and_honest():
    path = Path(__file__).parents[1] / "docs" / "phase4-model-card.json"
    assert json.loads(path.read_text(encoding="utf-8")) == not_evaluable_model_card()
    assert not_evaluable_model_card()["promotion"]["promote"] is False


@pytest.mark.parametrize("defect", ["future_event", "future_universe", "revision", "open_candle"])
def test_point_in_time_builder_rejects_each_required_leakage_class(defect: str):
    decision = 10_000
    feature = FeatureObservation(
        name="event_score", value=1.0, available_at_ms=decision - 1,
        source_kind="event", lineage_id="raw-event-1", published_at_ms=decision - 2,
    )
    candidate = row(10, feature)
    if defect == "future_event":
        candidate = replace(candidate, features=(replace(feature, available_at_ms=decision + 1),))
    elif defect == "future_universe":
        candidate = replace(candidate, universe_valid_from_ms=decision + 1)
    elif defect == "revision":
        candidate = replace(
            candidate, features=(replace(feature, revision_observed_at_ms=decision + 1),)
        )
    else:
        candle = replace(
            feature, source_kind="candle", candle_closed=False,
            candle_close_at_ms=decision + 60_000,
        )
        candidate = replace(candidate, features=(candle,))
    with pytest.raises(LeakageError):
        builder().build([candidate])


def test_point_in_time_universe_keeps_asset_delisted_after_decision():
    candidate = replace(row(10), universe_valid_to_ms=20_000)
    assert builder().build([candidate]).rows == (candidate,)


def test_snapshot_is_reproducible_and_tamper_evident(tmp_path: Path):
    first = builder().build([row(2), row(1)])
    second = builder().build([row(1), row(2)])
    assert first.snapshot_id == second.snapshot_id
    path = first.write(tmp_path / "snapshot.json")
    assert DatasetSnapshot.read(path).content_hash == first.content_hash
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["content"]["rows"][0]["features"][0]["value"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        DatasetSnapshot.read(path)


def test_walk_forward_purges_label_overlap_applies_embargo_and_freezes_test():
    rows = [row(index, end=40_000 if index == 19 else None) for index in range(100)]
    plan = PurgedWalkForwardSplitter(
        folds=2, test_fraction=0.20, minimum_train_rows=20,
        minimum_partition_rows=5, embargo_ms=100,
    ).split(rows)
    assert len(plan.folds) == 2
    assert "r19" not in plan.folds[0].train_ids
    assert "r20" not in plan.folds[0].validation_ids
    assert plan.folds[0].test_ids == plan.folds[1].test_ids == plan.untouched_test_ids
    for fold in plan.folds:
        partitions = [set(fold.train_ids), set(fold.validation_ids),
                      set(fold.calibration_ids), set(fold.test_ids)]
        assert not any(left & right for i, left in enumerate(partitions)
                       for right in partitions[i + 1:])


def test_forward_targets_cover_required_horizons_and_costs():
    path = [
        PricePoint(minutes * 60_000, 100 + minutes / 100, 101, 99, 10_000)
        for minutes in (5, 15, 60, 240, 1440)
    ]
    results = forward_outcomes(
        decision_at_ms=0, entry_price=100, path=path, round_trip_cost_bps=20,
        take_profit=0.005, stop_loss=0.005, required_exit_notional_usd=1_000,
    )
    assert {item.horizon for item in results} == {"5m", "15m", "1h", "4h", "24h"}
    assert all(item.net_return == pytest.approx(item.gross_return - 0.002) for item in results)
    assert all(item.exit_feasible for item in results)


def test_execution_costs_partial_fills_rejections_and_accounting_reconcile():
    assumptions = ExecutionAssumptions(maximum_liquidity_participation=0.05)
    partial = simulate_execution(TradeOpportunity(
        "partial", 1, 1_000, 100, 100.2, 10_000, 10_000,
    ), assumptions)
    assert partial.status == "PARTIAL" and 499 < partial.filled_notional_usd < 500
    assert partial.gross_pnl_usd > 0 > partial.net_pnl_usd
    assert partial.reconciles()
    rejected = simulate_execution(TradeOpportunity(
        "rejected", 1, 1_000, 100, 110, 1_000, 1_000,
    ), assumptions)
    assert rejected.status == "REJECTED_LIQUIDITY" and rejected.net_pnl_usd == 0
    short = simulate_execution(TradeOpportunity(
        "short", -1, 1_000, 100, 90, 100_000, 100_000,
    ), replace(assumptions, funding_bps=10))
    assert short.funding_usd < 0 and short.reconciles()


def test_two_stage_pipeline_is_segmented_and_risk_gated():
    candidates = [
        OpportunityCandidate("major-a", "a", "5m", 1, "a", "majors_l1", "normal",
                             .8, .02, -.01, .03, .02, .9, 1e6, True, {"x": 2}),
        OpportunityCandidate("major-b", "b", "5m", 1, "b", "majors_l1", "normal",
                             .8, .01, -.01, .02, .02, .9, 1e6, True, {"x": 2}),
        OpportunityCandidate("meme-a", "c", "5m", 1, "c", "new_memecoin", "volatile",
                             .6, .03, -.02, .05, .04, .8, 5e4, True, {"x": 3}),
        OpportunityCandidate("unsafe", "d", "5m", 1, "d", "new_memecoin", "volatile",
                             .99, .20, -.02, .2, .1, .8, 5e4, False, {"x": 9}),
    ]
    generated = CandidateGenerator(.5, {"new_memecoin:5m": .55}).generate(candidates)
    ranked = CrossSectionalRanker(top_k=1).rank(generated)
    assert {item.opportunity_id for item in ranked} == {"major-a", "meme-a"}


def test_negative_return_adverse_excursion_and_exit_risk_reject_candidates():
    safe = OpportunityCandidate(
        "safe", "safe", "5m", 1, "safe", "majors_l1", "normal",
        .9, .01, -.02, .03, .02, .9, 1_000_000, True, {"signal": 1},
    )
    rejected = [
        replace(safe, opportunity_id="negative", expected_net_return=-.001),
        replace(safe, opportunity_id="adverse", predicted_mae=-.20),
        replace(safe, opportunity_id="unexit", exit_feasibility_probability=.20),
    ]
    selected = CandidateGenerator(
        minimum_probability=.5, minimum_expected_return=0,
        minimum_exit_feasibility_probability=.6,
        maximum_predicted_adverse_excursion=.05,
        maximum_predicted_volatility=.2,
    ).generate([safe, *rejected])
    assert [item.opportunity_id for item in selected] == ["safe"]


def test_metrics_include_counts_uncertainty_calibration_and_regimes():
    records = [
        forecast(f"r{i}|5m", probability=.7, target=i % 3 != 0, selected=True,
                 gross_return=.01 if i % 3 else -.005, block=f"block-{i // 10}")
        for i in range(40)
    ]
    metrics = evaluate_records(records)
    assert metrics["sample_count"] == metrics["trade_count"] == 40
    assert len(metrics["net_expectancy_ci95"]) == 2
    assert 0 <= metrics["brier"] <= 1 and 0 <= metrics["ece"] <= 1
    assert metrics["time_block_count"] == 4
    assert metrics["segments"] == ["majors_l1"] and metrics["regimes"] == ["normal"]


def test_promotion_requires_uncertain_oos_edge_and_preserves_negative_result():
    policy = PromotionPolicy(minimum_test_samples=10, minimum_trades=5,
                             minimum_time_blocks=2, maximum_ece=.2)
    baseline = {
        "status": "EVALUATED", "sample_count": 100, "trade_count": 30,
        "net_expectancy": .001, "net_expectancy_ci95": [-.001, .004],
        "maximum_drawdown": .10, "brier": .20, "ece": .05,
        "time_block_count": 3, "time_blocks": {"a": {"net_expectancy": .001}},
    }
    weak = dict(baseline, net_expectancy=.002, net_expectancy_ci95=[-.002, .006])
    decision = decide_promotion(
        challenger_name="weak", challenger=weak, baselines={"momentum": baseline},
        incumbent="production", policy=policy,
    )
    assert not decision.promote and decision.status == "RETAIN_CHAMPION"
    strong = dict(
        baseline, sample_count=300, trade_count=80, net_expectancy=.02,
        net_expectancy_ci95=[.01, .03], maximum_drawdown=.05, brier=.10, ece=.03,
        time_blocks={"a": {"net_expectancy": .01}, "b": {"net_expectancy": .02},
                     "c": {"net_expectancy": .03}},
    )
    assert decide_promotion(
        challenger_name="strong", challenger=strong, baselines={"momentum": baseline},
        incumbent="production", policy=policy,
    ).promote


def test_honest_experiment_uses_only_test_ids_and_retains_ablations():
    rows = [row(index) for index in range(100)]
    plan = PurgedWalkForwardSplitter(
        folds=2, test_fraction=.2, minimum_train_rows=20, minimum_partition_rows=5,
    ).split(rows)
    records = [forecast(item, probability=.6, target=False, selected=True, gross_return=-.01)
               for item in plan.untouched_test_ids]
    no_trade = [replace(item, selected=False, tp_probability=0.0) for item in records]
    baselines = {name: no_trade for name in REQUIRED_BASELINES}
    result = run_champion_challenger(
        split_plan=plan, costs=ExecutionAssumptions(), baseline_records=baselines,
        challenger_name="candidate", challenger_records=records,
        ablation_records={"without_orderbook": records}, incumbent="production",
        policy=PromotionPolicy(minimum_test_samples=5, minimum_trades=5,
                               minimum_time_blocks=1), dataset_snapshot_id="snapshot",
    )
    assert not result.promotion.promote
    assert result.model_card["negative_results_preserved"] is True
    assert result.model_card["ablations"]["without_orderbook"]["net_expectancy"] < 0
    assert any(item["name"] == "without_orderbook"
               for item in result.model_card["rejected_variants"])
    with pytest.raises(ValueError, match="does not exactly match"):
        run_champion_challenger(
            split_plan=plan, costs=ExecutionAssumptions(), baseline_records=baselines,
            challenger_name="bad", challenger_records=[replace(records[0], opportunity_id="train")],
            ablation_records={}, incumbent="production", policy=PromotionPolicy(),
            dataset_snapshot_id="snapshot",
        )


def test_costs_change_expectancy_and_promotion_outcome():
    identifiers = tuple(f"test-{index}|5m" for index in range(60))
    plan = SplitPlan((), 0, True, identifiers)
    challenger = [
        forecast(identifier, probability=.9, target=True, selected=True,
                 gross_return=.01, block=f"block-{index // 20}")
        for index, identifier in enumerate(identifiers)
    ]
    no_trade = [replace(item, tp_probability=0.0, selected=False) for item in challenger]
    baselines = {name: no_trade for name in REQUIRED_BASELINES}
    ablations = {name: no_trade for name in REQUIRED_ABLATIONS}
    zero = ExecutionAssumptions(
        entry_fee_bps=0, exit_fee_bps=0, round_trip_spread_bps=0,
        round_trip_slippage_bps=0, latency_bps=0, funding_bps=0, impact_bps=0,
        maximum_liquidity_participation=1, minimum_fill_fraction=.25,
    )
    extreme = replace(
        zero, entry_fee_bps=100, exit_fee_bps=100, round_trip_spread_bps=100,
        round_trip_slippage_bps=100, latency_bps=100, impact_bps=100,
    )
    policy = PromotionPolicy(
        minimum_test_samples=10, minimum_trades=5, minimum_time_blocks=3,
        maximum_drawdown=1, maximum_ece=.2, require_all_blocks_positive=True,
    )
    free = run_champion_challenger(
        split_plan=plan, costs=zero, baseline_records=baselines,
        challenger_name="challenger", challenger_records=challenger,
        ablation_records=ablations, incumbent="production", policy=policy,
        dataset_snapshot_id="snapshot",
    )
    costly = run_champion_challenger(
        split_plan=plan, costs=extreme, baseline_records=baselines,
        challenger_name="challenger", challenger_records=challenger,
        ablation_records=ablations, incumbent="production", policy=policy,
        dataset_snapshot_id="snapshot",
    )
    assert free.challenger["net_expectancy"] == pytest.approx(.01)
    assert costly.challenger["net_expectancy"] == pytest.approx(-.0501)
    assert free.promotion.promote is True
    assert costly.promotion.promote is False


@pytest.mark.parametrize("missing", ["entry_fee_bps", "round_trip_slippage_bps"])
def test_incomplete_cost_model_fails_closed(missing: str):
    complete = ExecutionAssumptions().to_dict()
    complete.pop(missing)
    with pytest.raises(ValueError, match=missing):
        ExecutionAssumptions.from_mapping(complete, require_complete=True)


def test_tp_calibration_never_uses_positive_return_as_label():
    records = [
        forecast(f"r{index}|5m", probability=.01, target=False, selected=True,
                 gross_return=.10, block=f"block-{index // 10}")
        for index in range(30)
    ]
    metrics = evaluate_records(records, costs=ExecutionAssumptions(
        entry_fee_bps=0, exit_fee_bps=0, round_trip_spread_bps=0,
        round_trip_slippage_bps=0, latency_bps=0, funding_bps=0, impact_bps=0,
        maximum_liquidity_participation=1, minimum_fill_fraction=.25,
    ))
    assert metrics["target"] == "tp_before_stop"
    assert metrics["brier"] == pytest.approx(.0001)
    assert metrics["tp_precision"] == 0
    assert metrics["net_win_rate"] == 1


def test_forecast_requires_explicit_horizon_and_test_vault_is_one_shot():
    with pytest.raises(ValueError, match="explicit horizon"):
        forecast("bad", horizon="")
    vault = UntouchedTestVault([row(1), row(2)])
    with pytest.raises(UntouchedTestAccessError, match="before selection"):
        vault.open(["r1"], selection_frozen=False)
    assert [item.row_id for item in vault.open(["r1"], selection_frozen=True)] == ["r1"]
    with pytest.raises(UntouchedTestAccessError, match="exactly once"):
        vault.open(["r2"], selection_frozen=True)


def _end_to_end_snapshot(tmp_path: Path) -> tuple[Path, dict]:
    rows = []
    for index in range(80):
        decision = index * 600_000
        positive = index % 2 == 0
        signal = 1.0 if positive else -1.0
        features = (
            FeatureObservation("closed_return", signal, decision - 1, "candle", f"c-{index}",
                               candle_close_at_ms=decision - 1, candle_closed=True),
            FeatureObservation("momentum", signal, decision - 1, "technical", f"m-{index}"),
            FeatureObservation("btc_beta", signal * .5, decision - 1, "technical", f"b-{index}"),
            FeatureObservation("current_production_probability", .8 if positive else .2,
                               decision - 1, "production", f"p-{index}"),
            FeatureObservation("news_score", signal, decision - 1, "news", f"n-{index}"),
            FeatureObservation("social_score", signal, decision - 1, "social", f"s-{index}"),
            FeatureObservation("onchain_score", signal, decision - 1, "onchain", f"o-{index}"),
            FeatureObservation("book_score", signal, decision - 1, "orderbook", f"l-{index}"),
            FeatureObservation("flow_score", signal, decision - 1, "trade_flow", f"t-{index}"),
            FeatureObservation("derivatives_score", signal, decision - 1, "derivatives", f"d-{index}"),
            FeatureObservation("truth_score", signal, decision - 1, "market_truth", f"x-{index}"),
            FeatureObservation("risk_eligible", True, decision - 1, "risk", f"r-{index}"),
            FeatureObservation("decision_liquidity_usd", 1_000_000, decision - 1,
                               "market_truth", f"q-{index}"),
        )
        target = ForwardOutcome(
            horizon="5m", horizon_ms=300_000, label_end_at_ms=decision + 300_000,
            gross_return=.01 if positive else -.01,
            net_return=.008 if positive else -.012, tp_before_stop=positive,
            mae=-.005, mfe=.012, realized_volatility=.01, exit_feasible=True,
            direction=1, entry_price=100, exit_price=101 if positive else 99,
            entry_liquidity_usd=1_000_000, exit_liquidity_usd=1_000_000,
        )
        rows.append(ResearchRow(
            row_id=f"e2e-{index}", instrument_id="venue:spot:SOL-USD",
            canonical_asset_id="native:solana:SOL", universe_bucket="majors_l1",
            regime="normal", decision_at_ms=decision, universe_valid_from_ms=0,
            universe_valid_to_ms=None, instrument_active_at_decision=True,
            features=features, outcomes=(target,),
        ))
    snapshot = CausalDatasetBuilder(
        code_version="synthetic-test-only", config={"fixture": True},
        feature_schema={feature.name: "float64" for feature in rows[0].features},
        universe_definition={"fixture": True},
    ).build(rows)
    path = snapshot.write(tmp_path / "snapshot.json")
    config = {
        "research": {
            "horizons": ["5m", "15m", "1h", "4h", "24h"],
            "walk_forward_folds": 1, "untouched_test_fraction": .2,
            "embargo_seconds": 0, "minimum_train_rows": 20,
            "minimum_partition_rows": 5, "top_k": 3,
            "requested_notional_usd": 1000,
            "threshold_candidates": [.45, .55, .65],
            "regularization_candidates": [.1, 1.0], "random_state": 7,
            "ranking_gates": {
                "minimum_predicted_net_return": 0.0,
                "minimum_exit_feasibility_probability": .6,
                "maximum_predicted_adverse_excursion": .05,
                "maximum_predicted_volatility": .2,
            },
            "promotion": {
                "minimum_test_samples": 10, "minimum_trades": 2,
                "minimum_time_blocks": 1, "maximum_drawdown": 1,
                "maximum_ece": 1, "require_all_blocks_positive": False,
            },
            "execution_costs": ExecutionAssumptions().to_dict(),
        }
    }
    return path, config


def test_return_head_is_cost_sensitive_and_test_labels_cannot_affect_fit(tmp_path: Path):
    path, config = _end_to_end_snapshot(tmp_path)
    snapshot = DatasetSnapshot.read(path)
    runner_config = RunnerConfig.from_mapping(config["research"])
    task_rows = _task_rows(snapshot, "majors_l1", "5m")
    plan = PurgedWalkForwardSplitter(
        folds=runner_config.folds, test_fraction=runner_config.test_fraction,
        minimum_train_rows=runner_config.minimum_train_rows,
        minimum_partition_rows=runner_config.minimum_partition_rows,
        embargo_ms=runner_config.embargo_ms,
    ).split(task_rows)
    development = {
        item.row_id: item for item in task_rows
        if item.row_id not in plan.untouched_test_ids
    }
    zero = ExecutionAssumptions(
        entry_fee_bps=0, exit_fee_bps=0, round_trip_spread_bps=0,
        round_trip_slippage_bps=0, latency_bps=0, funding_bps=0, impact_bps=0,
        maximum_liquidity_participation=1, minimum_fill_fraction=.25,
    )
    extreme = replace(
        zero, entry_fee_bps=100, exit_fee_bps=100, round_trip_spread_bps=100,
        round_trip_slippage_bps=100, latency_bps=100, impact_bps=100,
    )
    free_heads = _fit_predictive_heads(
        rows_by_id=development, plan=plan, horizon="5m",
        config=runner_config, costs=zero,
    )
    costly_heads = _fit_predictive_heads(
        rows_by_id=development, plan=plan, horizon="5m",
        config=runner_config, costs=extreme,
    )
    test_row = next(item for item in task_rows if item.row_id == plan.untouched_test_ids[0])
    free_return = float(free_heads.expected_return.predict([test_row])[0])
    costly_return = float(costly_heads.expected_return.predict([test_row])[0])
    assert free_return > 0 > costly_return
    probability = float(free_heads.tp.predict([test_row])[0])
    mae = float(free_heads.mae.predict([test_row])[0])
    mfe = float(free_heads.mfe.predict([test_row])[0])
    volatility = float(free_heads.realized_volatility.predict([test_row])[0])
    exit_probability = float(free_heads.exit_feasibility.predict([test_row])[0])
    candidate = OpportunityCandidate(
        "cost-sensitive", test_row.row_id, "5m", test_row.decision_at_ms,
        test_row.instrument_id, test_row.universe_bucket, test_row.regime,
        probability, free_return, mae, mfe, volatility, exit_probability,
        1_000_000, True, {"signal": probability},
    )
    generator = CandidateGenerator(
        minimum_probability=.45, minimum_expected_return=0,
        minimum_exit_feasibility_probability=.6,
        maximum_predicted_adverse_excursion=.05,
        maximum_predicted_volatility=.2,
    )
    assert generator.generate([candidate])
    assert not generator.generate([replace(candidate, expected_net_return=costly_return)])

    mutated_rows = []
    test_ids = set(plan.untouched_test_ids)
    for item in snapshot.rows:
        if item.row_id not in test_ids:
            mutated_rows.append(item)
            continue
        target = item.outcomes[0]
        changed = replace(
            target, tp_before_stop=not bool(target.tp_before_stop), gross_return=.5,
            entry_price=100, exit_price=150, mae=-.8, mfe=1.0,
            realized_volatility=1.0, exit_feasible=False,
        )
        future_only = FeatureObservation(
            "test_only_feature", 999, item.decision_at_ms - 1,
            "technical", f"future-{item.row_id}",
        )
        mutated_rows.append(replace(
            item, features=(*item.features, future_only), outcomes=(changed,)
        ))
    mutated = replace(snapshot, rows=tuple(mutated_rows))
    mutated_task = _task_rows(mutated, "majors_l1", "5m")
    mutated_development = {
        item.row_id: item for item in mutated_task
        if item.row_id not in plan.untouched_test_ids
    }
    unaffected_heads = _fit_predictive_heads(
        rows_by_id=mutated_development, plan=plan, horizon="5m",
        config=runner_config, costs=zero,
    )
    probe = [development[plan.folds[-1].calibration_ids[0]]]
    assert free_heads.tp.threshold == unaffected_heads.tp.threshold
    assert free_heads.expected_return.feature_names == unaffected_heads.expected_return.feature_names
    assert "test_only_feature" not in free_heads.expected_return.feature_names
    np.testing.assert_allclose(
        free_heads.expected_return.predict(probe),
        unaffected_heads.expected_return.predict(probe), rtol=0, atol=0,
    )
    np.testing.assert_allclose(
        free_heads.tp.predict(probe), unaffected_heads.tp.predict(probe),
        rtol=0, atol=0,
    )


def test_cli_end_to_end_snapshot_fit_calibration_execution_registry_and_card(tmp_path: Path):
    snapshot, config = _end_to_end_snapshot(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    database = tmp_path / "research.db"
    card_path = tmp_path / "model-card.json"
    root = Path(__file__).parents[1]
    command = [
        sys.executable, "research.py", "--snapshot", str(snapshot),
        "--config", str(config_path), "--db", str(database),
        "--output", str(card_path),
    ]
    completed = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    repeated = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=30, check=False,
    )
    assert repeated.returncode == 0, repeated.stderr
    summary = json.loads(completed.stdout)
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert summary["status"] == card["status"] == "EVALUATED"
    assert summary["promotion"] is card["promotion"]["promote"] is False
    assert summary["test_access_count"] == card["test_access_count"] == 1
    assert card["real_execution_enabled"] is False
    assert card["real_performance_claim"] is False
    assert card["dataset_universe_definition"] == {"fixture": True}
    assert card["task_metrics"]["majors_l1:5m"]["target"] == "tp_before_stop"
    assert card["task_metrics"]["majors_l1:5m"]["horizons"] == ["5m"]
    assert {key: card["split_sizes"]["majors_l1:5m"][key]
            for key in ("train", "validation", "calibration", "test")} == {
                "train": 20, "validation": 22, "calibration": 22, "test": 16,
            }
    assert card["task_models"]["majors_l1:5m"]["challenger"][
        "tp_before_stop"
    ]["calibrated"] is True
    assert card["task_models"]["majors_l1:5m"]["challenger"][
        "execution_adjusted_return"
    ]["units"] == "return_fraction_on_requested_notional"
    assert set(card["baselines"]) == set(REQUIRED_BASELINES)
    assert set(card["ablations"]) == set(REQUIRED_ABLATIONS)
    storage = SQLiteStorage(database)
    try:
        saved = ResearchRegistry(storage).experiment(summary["experiment_id"])
        assert saved["model_card_json"] == card
        assert storage.query("SELECT COUNT(*) AS n FROM research_experiments")[0]["n"] == 1
        assert storage.health()["integrity"] == "ok"
    finally:
        storage.close()


def test_schema_v7_to_v8_registry_is_additive_immutable_and_reproducible(tmp_path: Path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
      CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
      INSERT INTO schema_migrations VALUES (7, 1);
      CREATE TABLE signals(
        id INTEGER PRIMARY KEY, coin TEXT NOT NULL, timestamp INTEGER NOT NULL,
        primary_horizon TEXT NOT NULL, direction TEXT NOT NULL, probability REAL NOT NULL,
        payload_json TEXT NOT NULL, model_status TEXT NOT NULL
      );
      INSERT INTO signals VALUES (1,'SOL',1,'1h','WAIT',50,'{}','MODEL_REJECTED_OOS');
    """)
    connection.commit()
    connection.close()
    storage = SQLiteStorage(database)
    try:
        assert SCHEMA_VERSION == storage.health()["schema_version"] == 13
        assert storage.health()["integrity"] == "ok"
        assert storage.query("SELECT coin FROM signals") == [{"coin": "SOL"}]
        snapshot = builder().build([row(1), row(2)])
        path = snapshot.write(tmp_path / "snapshot.json")
        registry = ResearchRegistry(storage)
        registry.register_snapshot(snapshot, path, created_at_ms=3_000)
        assert registry.reproduce_snapshot(snapshot.snapshot_id).content_hash == snapshot.content_hash
        card = {"limitations": ["fixture only"]}
        registry.register_experiment(ExperimentRecord(
            "experiment", snapshot.snapshot_id, 4_000, "NOT_EVALUABLE", {},
            ExecutionAssumptions().to_dict(), {}, {"sample_count": 0}, card,
            {"promote": False, "status": "NOT_EVALUABLE"},
        ))
        saved = registry.experiment("experiment")
        assert saved["promoted"] == 0
        assert saved["snapshot_id"] == snapshot.snapshot_id
        assert saved["model_card_json"] == card
        assert saved["challenger_metrics_json"] == {"sample_count": 0}
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            storage.execute(
                "UPDATE research_experiments SET status='EVALUATED' WHERE experiment_id='experiment'"
            )
    finally:
        storage.close()
