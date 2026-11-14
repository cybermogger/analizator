from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from .execution import ExecutionAssumptions, TradeOpportunity, simulate_execution


@dataclass(frozen=True)
class OpportunityCandidate:
    opportunity_id: str
    row_id: str
    horizon: str
    decision_at_ms: int
    instrument_id: str
    segment: str
    regime: str
    tp_probability: float
    expected_net_return: float
    predicted_mae: float
    predicted_mfe: float
    predicted_volatility: float
    exit_feasibility_probability: float
    liquidity_usd: float
    risk_eligible: bool
    signals: Mapping[str, float]

    def __post_init__(self) -> None:
        if not self.horizon:
            raise ValueError("candidate horizon is required")
        if not 0.0 <= self.tp_probability <= 1.0:
            raise ValueError("candidate probability must be in [0,1]")
        if self.liquidity_usd < 0:
            raise ValueError("candidate liquidity cannot be negative")
        if not 0.0 <= self.exit_feasibility_probability <= 1.0:
            raise ValueError("exit feasibility probability must be in [0,1]")


@dataclass(frozen=True)
class ForecastRecord:
    """A frozen prediction plus raw execution inputs and evaluated outcome fields."""

    opportunity_id: str
    row_id: str
    horizon: str
    time_block: str
    segment: str
    regime: str
    tp_probability: float
    tp_before_stop_target: bool
    selected: bool
    direction: int
    requested_notional_usd: float
    entry_price: float
    exit_price: float
    entry_liquidity_usd: float
    exit_liquidity_usd: float
    expected_net_return: float = 0.0
    predicted_mae: float = 0.0
    predicted_mfe: float = 0.0
    predicted_volatility: float = 0.0
    exit_feasibility_probability: float = 0.0
    realized_mae: float = 0.0
    realized_mfe: float = 0.0
    realized_volatility: float = 0.0
    exit_feasible_target: bool = False
    execution_adjusted_return_target: float | None = None
    execution_status: str = "NOT_SIMULATED"
    realized_net_return: float | None = None
    gross_pnl_usd: float | None = None
    net_pnl_usd: float | None = None
    fees_usd: float | None = None
    spread_usd: float | None = None
    slippage_usd: float | None = None
    latency_usd: float | None = None
    funding_usd: float | None = None
    impact_usd: float | None = None
    filled_notional_usd: float | None = None

    def __post_init__(self) -> None:
        if not self.horizon:
            raise ValueError("every forecast must have an explicit horizon")
        if not 0.0 <= self.tp_probability <= 1.0:
            raise ValueError("forecast probability must be in [0,1]")
        if self.direction not in (-1, 1):
            raise ValueError("forecast direction must be -1 or 1")

    @property
    def predicted_probability(self) -> float:
        """Compatibility alias; calibration is always TP-before-stop probability."""

        return self.tp_probability


class CandidateGenerator:
    """High-recall causal screen; ranking is deliberately a separate stage."""

    def __init__(
        self,
        minimum_probability: float = 0.0,
        task_thresholds: Mapping[str, float] | None = None,
        minimum_expected_return: float = 0.0,
        minimum_exit_feasibility_probability: float = 0.5,
        maximum_predicted_adverse_excursion: float = 0.05,
        maximum_predicted_volatility: float = 1.0,
    ) -> None:
        self.minimum_probability = minimum_probability
        self.task_thresholds = dict(task_thresholds or {})
        self.minimum_expected_return = minimum_expected_return
        self.minimum_exit_feasibility_probability = minimum_exit_feasibility_probability
        self.maximum_predicted_adverse_excursion = maximum_predicted_adverse_excursion
        self.maximum_predicted_volatility = maximum_predicted_volatility

    def generate(self, candidates: Iterable[OpportunityCandidate]) -> list[OpportunityCandidate]:
        selected: list[OpportunityCandidate] = []
        for item in candidates:
            task = f"{item.segment}:{item.horizon}"
            threshold = self.task_thresholds.get(
                task, self.task_thresholds.get(item.segment, self.minimum_probability)
            )
            if (
                item.risk_eligible
                and item.liquidity_usd > 0
                and item.tp_probability >= threshold
                and item.expected_net_return > self.minimum_expected_return
                and item.exit_feasibility_probability
                >= self.minimum_exit_feasibility_probability
                and item.predicted_mae >= -self.maximum_predicted_adverse_excursion
                and item.predicted_volatility <= self.maximum_predicted_volatility
            ):
                selected.append(item)
        return selected


class CrossSectionalRanker:
    """Rank within decision/segment/horizon, never across incomparable tasks."""

    def __init__(self, top_k: int = 3) -> None:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        self.top_k = top_k

    def rank(self, candidates: Iterable[OpportunityCandidate]) -> list[OpportunityCandidate]:
        grouped: dict[tuple[int, str, str], list[OpportunityCandidate]] = {}
        for item in candidates:
            grouped.setdefault(
                (item.decision_at_ms, item.segment, item.horizon), []
            ).append(item)
        selected: list[OpportunityCandidate] = []
        for key in sorted(grouped):
            ranked = sorted(
                grouped[key],
                key=lambda item: (
                    item.expected_net_return,
                    item.tp_probability,
                    item.exit_feasibility_probability,
                    item.predicted_mfe,
                    item.liquidity_usd,
                ),
                reverse=True,
            )
            selected.extend(ranked[:self.top_k])
        return selected


def apply_execution(
    records: Iterable[ForecastRecord],
    costs: ExecutionAssumptions,
) -> list[ForecastRecord]:
    """Apply the declared cost model; pre-computed net returns are never trusted."""

    evaluated: list[ForecastRecord] = []
    for record in records:
        result = simulate_execution(TradeOpportunity(
            opportunity_id=record.opportunity_id,
            direction=record.direction,
            requested_notional_usd=record.requested_notional_usd,
            entry_price=record.entry_price,
            exit_price=record.exit_price,
            entry_liquidity_usd=record.entry_liquidity_usd,
            exit_liquidity_usd=record.exit_liquidity_usd,
        ), costs)
        target_return = result.net_pnl_usd / record.requested_notional_usd
        if not record.selected:
            evaluated.append(replace(
                record, execution_status="NO_TRADE", realized_net_return=0.0,
                execution_adjusted_return_target=target_return,
                gross_pnl_usd=0.0, net_pnl_usd=0.0, fees_usd=0.0,
                spread_usd=0.0, slippage_usd=0.0, latency_usd=0.0,
                funding_usd=0.0, impact_usd=0.0, filled_notional_usd=0.0,
            ))
            continue
        evaluated.append(replace(
            record,
            execution_status=result.status,
            # Opportunity expectancy is measured against requested capital, so
            # partial fills reduce the realized return instead of being scaled
            # up as though the full request had filled.
            realized_net_return=result.net_pnl_usd / record.requested_notional_usd,
            execution_adjusted_return_target=target_return,
            gross_pnl_usd=result.gross_pnl_usd,
            net_pnl_usd=result.net_pnl_usd,
            fees_usd=result.fees_usd,
            spread_usd=result.spread_usd,
            slippage_usd=result.slippage_usd,
            latency_usd=result.latency_usd,
            funding_usd=result.funding_usd,
            impact_usd=result.impact_usd,
            filled_notional_usd=result.filled_notional_usd,
        ))
    return evaluated


def _ece(records: list[ForecastRecord], bins: int = 10) -> float:
    if not records:
        return math.nan
    total = len(records)
    value = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        bucket = [item for item in records if low <= item.tp_probability < high
                  or index == bins - 1 and item.tp_probability == 1.0]
        if not bucket:
            continue
        predicted = sum(item.tp_probability for item in bucket) / len(bucket)
        actual = sum(item.tp_before_stop_target for item in bucket) / len(bucket)
        value += len(bucket) / total * abs(predicted - actual)
    return value


def _maximum_drawdown(returns: list[float]) -> float:
    equity = peak = 0.0
    drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _bootstrap_interval(values: list[float], *, seed: int, samples: int = 400) -> list[float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        draw = [values[rng.randrange(len(values))] for _ in values]
        estimates.append(sum(draw) / len(draw))
    estimates.sort()
    return [estimates[int(samples * 0.025)], estimates[int(samples * 0.975)]]


def _time_block_interval(
    records: list[ForecastRecord],
    *,
    target: str,
    seed: int,
    samples: int = 400,
) -> list[float] | None:
    grouped: dict[str, list[ForecastRecord]] = {}
    for record in records:
        grouped.setdefault(record.time_block, []).append(record)
    blocks = list(grouped.values())

    def values(items: list[ForecastRecord]) -> list[float]:
        if target == "tp":
            return [1.0 if item.tp_before_stop_target else 0.0 for item in items]
        return [float(item.realized_net_return or 0.0) for item in items]

    raw = values(records)
    if len(blocks) < 2:
        return _bootstrap_interval(raw, seed=seed, samples=samples)
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(samples):
        draw = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        sampled = values([record for block in draw for record in block])
        estimates.append(sum(sampled) / len(sampled))
    estimates.sort()
    return [estimates[int(samples * 0.025)], estimates[int(samples * 0.975)]]


def evaluate_records(
    records: Iterable[ForecastRecord],
    *,
    costs: ExecutionAssumptions | None = None,
    top_k: int = 3,
    bootstrap_seed: int = 42,
) -> dict:
    values = apply_execution(records, costs or ExecutionAssumptions())
    if not values:
        return {
            "status": "NOT_EVALUABLE", "sample_count": 0, "trade_count": 0,
            "net_expectancy_ci95": None, "precision_ci95": None,
            "reason": "no untouched out-of-sample observations",
        }
    selected = [item for item in values if item.selected]
    executed = [item for item in selected if item.execution_status in {"FILLED", "PARTIAL"}]
    returns = [float(item.realized_net_return or 0.0) for item in selected]
    positives = [value for value in returns if value > 0]
    negatives = [-value for value in returns if value < 0]
    actual = [1.0 if item.tp_before_stop_target else 0.0 for item in values]
    brier = sum((item.tp_probability - label) ** 2
                for item, label in zip(values, actual, strict=True)) / len(values)
    precision = sum(item.tp_before_stop_target for item in selected) / len(selected) \
        if selected else 0.0
    expectancy = sum(returns) / len(returns) if returns else 0.0
    blocks: dict[str, list[float]] = {}
    for item in selected:
        blocks.setdefault(item.time_block, []).append(float(item.realized_net_return or 0.0))
    block_metrics = {
        key: {"sample_count": len(block), "net_expectancy": sum(block) / len(block)}
        for key, block in sorted(blocks.items())
    }
    top: list[ForecastRecord] = []
    grouped: dict[tuple[str, str, str], list[ForecastRecord]] = {}
    for item in values:
        grouped.setdefault((item.time_block, item.segment, item.horizon), []).append(item)
    for group in grouped.values():
        top.extend(sorted(group, key=lambda item: item.tp_probability, reverse=True)[:top_k])
    precision_at_k = sum(item.tp_before_stop_target for item in top) / len(top)
    gross = [float(item.gross_pnl_usd or 0.0) / item.requested_notional_usd
             for item in selected]
    return_errors = [
        item.expected_net_return - float(item.execution_adjusted_return_target or 0.0)
        for item in values
    ]
    mae_errors = [item.predicted_mae - item.realized_mae for item in values]
    mfe_errors = [item.predicted_mfe - item.realized_mfe for item in values]
    volatility_errors = [
        item.predicted_volatility - item.realized_volatility for item in values
    ]
    return {
        "status": "EVALUATED",
        "target": "tp_before_stop",
        "sample_count": len(values),
        "selected_count": len(selected),
        "trade_count": len(executed),
        "rejected_count": sum(item.execution_status == "REJECTED_LIQUIDITY" for item in selected),
        "gross_expectancy": sum(gross) / len(gross) if gross else 0.0,
        "net_expectancy": expectancy,
        "net_expectancy_ci95": _time_block_interval(
            selected, target="net", seed=bootstrap_seed
        ),
        "tp_precision": precision,
        "precision": precision,
        "precision_ci95": _time_block_interval(
            selected, target="tp", seed=bootstrap_seed + 1
        ),
        "precision_at_k": precision_at_k,
        "net_win_rate": sum(value > 0 for value in returns) / len(returns) if returns else 0.0,
        "profit_factor": sum(positives) / sum(negatives) if negatives else None,
        "turnover_adjusted_return": sum(returns) / max(
            sum((item.filled_notional_usd or 0.0) / item.requested_notional_usd
                for item in selected), 1.0
        ),
        "maximum_drawdown": _maximum_drawdown(returns),
        "brier": brier,
        "ece": _ece(values),
        "false_alert_rate": 1.0 - precision if selected else 0.0,
        "coverage": len(selected) / len(values),
        "time_block_count": len(block_metrics),
        "time_blocks": block_metrics,
        "segments": sorted({item.segment for item in values}),
        "regimes": sorted({item.regime for item in values}),
        "horizons": sorted({item.horizon for item in values}),
        "mean_predicted_net_return": sum(item.expected_net_return for item in values) / len(values),
        "mean_predicted_mae": sum(item.predicted_mae for item in values) / len(values),
        "mean_predicted_mfe": sum(item.predicted_mfe for item in values) / len(values),
        "mean_predicted_volatility": sum(item.predicted_volatility for item in values) / len(values),
        "mean_exit_feasibility_probability": sum(
            item.exit_feasibility_probability for item in values
        ) / len(values),
        "predictive_head_metrics": {
            "execution_adjusted_return_rmse": math.sqrt(
                sum(value * value for value in return_errors) / len(return_errors)
            ),
            "mae_rmse": math.sqrt(sum(value * value for value in mae_errors) / len(mae_errors)),
            "mfe_rmse": math.sqrt(sum(value * value for value in mfe_errors) / len(mfe_errors)),
            "realized_volatility_rmse": math.sqrt(
                sum(value * value for value in volatility_errors) / len(volatility_errors)
            ),
            "exit_feasibility_brier": sum(
                (item.exit_feasibility_probability - float(item.exit_feasible_target)) ** 2
                for item in values
            ) / len(values),
        },
        "execution": {
            "fees_usd": sum(float(item.fees_usd or 0.0) for item in selected),
            "spread_usd": sum(float(item.spread_usd or 0.0) for item in selected),
            "slippage_usd": sum(float(item.slippage_usd or 0.0) for item in selected),
            "latency_usd": sum(float(item.latency_usd or 0.0) for item in selected),
            "funding_usd": sum(float(item.funding_usd or 0.0) for item in selected),
            "impact_usd": sum(float(item.impact_usd or 0.0) for item in selected),
        },
    }


def metrics_to_dict(metrics: Mapping[str, object]) -> dict:
    return dict(metrics)
