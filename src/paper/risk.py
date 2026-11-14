from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .types import PaperOpportunity, RiskContext, RiskDecision


@dataclass(frozen=True)
class PaperRiskConfig:
    shadow_mode: bool = True
    initial_equity_usd: float = 100_000.0
    major_risk_fraction: float = 0.0025
    memecoin_risk_fraction: float = 0.0005
    daily_loss_limit_fraction: float = 0.015
    maximum_paper_risk_usd: float = 1_000.0
    maximum_gross_exposure_fraction: float = 0.50
    maximum_cluster_exposure_fraction: float = 0.15
    maximum_concurrent_positions: int = 8
    maximum_liquidity_participation: float = 0.01
    minimum_liquidity_usd: float = 25_000.0
    minimum_exit_feasibility_probability: float = 0.60
    maximum_data_age_seconds: float = 180.0
    maximum_venue_divergence_bps: float = 150.0
    maximum_predicted_volatility: float = 0.20
    maximum_predicted_mae: float = 0.05
    minimum_calibrated_probability: float = 0.50
    volatility_risk_multiplier: float = 1.0
    maximum_gap_bps: float = 500.0
    cooldown_seconds: int = 900
    recommendation_ttl_seconds: int = 1800
    entry_fee_bps: float = 4.0
    exit_fee_bps: float = 4.0
    liquidity_loss_fraction: float = 0.50
    model_drift_min_samples: int = 100
    model_drift_min_balanced_accuracy: float = 0.30
    model_drift_max_brier: float = 0.75
    model_drift_max_ece: float = 0.15

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "PaperRiskConfig":
        values = dict(values or {})
        known = {name: values[name] for name in cls.__dataclass_fields__ if name in values}
        config = cls(**known)
        if not config.shadow_mode:
            raise ValueError("Phase 5 supports SHADOW mode only")
        fractions = (
            config.major_risk_fraction, config.memecoin_risk_fraction,
            config.daily_loss_limit_fraction, config.maximum_gross_exposure_fraction,
            config.maximum_cluster_exposure_fraction, config.maximum_liquidity_participation,
        )
        if any(value <= 0 or value > 1 for value in fractions):
            raise ValueError("paper risk fractions must be in (0,1]")
        if config.memecoin_risk_fraction > config.major_risk_fraction:
            raise ValueError("memecoin paper risk cannot exceed liquid-asset risk")
        if config.initial_equity_usd <= 0 or config.maximum_paper_risk_usd <= 0:
            raise ValueError("paper equity and maximum paper risk must be positive")
        if not 0 <= config.minimum_calibrated_probability <= 1:
            raise ValueError("minimum calibrated probability must be in [0,1]")
        if config.maximum_concurrent_positions < 1 or config.cooldown_seconds < 0:
            raise ValueError("position limit must be positive and cooldown nonnegative")
        if config.volatility_risk_multiplier <= 0:
            raise ValueError("volatility risk multiplier must be positive")
        if not 0 < config.liquidity_loss_fraction < 1:
            raise ValueError("liquidity loss fraction must be in (0,1)")
        return config


class PaperRiskEngine:
    def __init__(self, config: PaperRiskConfig):
        self.config = config

    def evaluate(self, opportunity: PaperOpportunity, context: RiskContext) -> RiskDecision:
        cfg = self.config
        reasons: list[str] = []
        now_ms = context.now_ms or opportunity.decision_at_ms
        if opportunity.direction == "NO_TRADE" or not opportunity.risk_eligible:
            reasons.append("NOT_RANKED_ELIGIBLE")
        if now_ms - opportunity.decision_at_ms > cfg.recommendation_ttl_seconds * 1000:
            reasons.append("OPPORTUNITY_EXPIRED")
        if opportunity.calibrated_probability < cfg.minimum_calibrated_probability:
            reasons.append("CALIBRATED_PROBABILITY_TOO_LOW")
        if opportunity.expected_net_move <= 0:
            reasons.append("NON_POSITIVE_EXPECTED_NET_MOVE")
        if opportunity.data_freshness_seconds > cfg.maximum_data_age_seconds or context.feed_stale:
            reasons.append("STALE_FEED")
        if context.provider_disagreement:
            reasons.append("PROVIDER_DISAGREEMENT")
        if context.primary_venue_outage:
            reasons.append("PRIMARY_VENUE_OUTAGE")
        if opportunity.venue_divergence_bps > cfg.maximum_venue_divergence_bps:
            reasons.append("VENUE_DIVERGENCE")
        if opportunity.predicted_volatility > cfg.maximum_predicted_volatility:
            reasons.append("EXTREME_VOLATILITY")
        if opportunity.predicted_mae < -cfg.maximum_predicted_mae:
            reasons.append("ADVERSE_EXCURSION")
        if context.extreme_gap or opportunity.spread_bps > cfg.maximum_gap_bps:
            reasons.append("EXTREME_GAP")
        if context.model_drift:
            reasons.append("MODEL_DRIFT")
        if context.liquidity_loss or opportunity.liquidity_usd < cfg.minimum_liquidity_usd:
            reasons.append("LIQUIDITY_LOSS")
        if opportunity.exit_feasibility_probability < cfg.minimum_exit_feasibility_probability:
            reasons.append("EXIT_NOT_FEASIBLE")
        if not opportunity.news_confirmed:
            reasons.append("UNCONFIRMED_NEWS")
        if opportunity.evidence_retracted:
            reasons.append("EVIDENCE_RETRACTED")
        if context.daily_net_pnl_usd <= -context.paper_equity_usd * cfg.daily_loss_limit_fraction:
            reasons.append("DAILY_LOSS_LIMIT")
        if context.open_position_count >= cfg.maximum_concurrent_positions:
            reasons.append("CONCURRENT_EXPOSURE_LIMIT")
        last = context.last_decision_at_ms.get(opportunity.instrument_id)
        if last is not None and now_ms - last < cfg.cooldown_seconds * 1000:
            reasons.append("COOLDOWN")

        risk_fraction = (
            cfg.memecoin_risk_fraction if opportunity.segment == "new_memecoin"
            else cfg.major_risk_fraction
        )
        paper_risk = min(context.paper_equity_usd * risk_fraction, cfg.maximum_paper_risk_usd)
        entry = sum(opportunity.entry_zone) / 2
        stop_fraction = abs(entry - opportunity.invalidation) / entry
        if stop_fraction <= 0:
            reasons.append("INVALID_INVALIDATION")
            notional = 0.0
        else:
            sizing_distance = max(
                stop_fraction, opportunity.predicted_volatility * cfg.volatility_risk_multiplier
            )
            notional = paper_risk / sizing_distance
        liquidity_cap = opportunity.liquidity_usd * cfg.maximum_liquidity_participation
        gross_remaining = max(
            0.0, context.paper_equity_usd * cfg.maximum_gross_exposure_fraction
            - context.current_gross_exposure_usd,
        )
        cluster_remaining = max(
            0.0, context.paper_equity_usd * cfg.maximum_cluster_exposure_fraction
            - float(context.cluster_exposure_usd.get(opportunity.correlation_cluster, 0.0)),
        )
        if gross_remaining <= 0:
            reasons.append("GROSS_EXPOSURE_LIMIT")
        if cluster_remaining <= 0:
            reasons.append("CORRELATED_EXPOSURE_LIMIT")
        notional = min(notional, liquidity_cap, gross_remaining, cluster_remaining)
        if notional <= 0:
            reasons.append("EXPOSURE_CAP")
        return RiskDecision(not reasons, tuple(dict.fromkeys(reasons)), paper_risk, max(notional, 0.0))
