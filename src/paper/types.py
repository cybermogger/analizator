from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class Evidence:
    category: str
    source_id: str
    summary: str
    url: str | None
    credibility: float
    freshness_seconds: float
    lineage_ids: tuple[str, ...] = ()
    confirmed: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.credibility <= 1:
            raise ValueError("evidence credibility must be in [0,1]")
        if self.freshness_seconds < 0:
            raise ValueError("evidence freshness cannot be negative")


@dataclass(frozen=True)
class PaperOpportunity:
    opportunity_id: str
    canonical_asset_id: str
    instrument_id: str
    segment: str
    direction: str
    decision_at_ms: int
    horizon: str
    calibrated_probability: float
    expected_net_move: float
    entry_zone: tuple[float, float]
    invalidation: float
    scenarios: tuple[str, ...]
    targets: tuple[float, ...]
    spread_bps: float
    liquidity_usd: float
    estimated_slippage_bps: float
    venue_divergence_bps: float
    predicted_volatility: float
    predicted_mae: float
    exit_feasibility_probability: float
    chain_id: str | None = None
    contract_address: str | None = None
    venue: str | None = None
    supporting_evidence: tuple[Evidence, ...] = ()
    contradicting_evidence: tuple[Evidence, ...] = ()
    alternative_scenario: str = "Insufficient evidence; remain in cash."
    source_credibility: float = 0.0
    data_freshness_seconds: float = 0.0
    model_name: str = "unknown"
    model_version: str = "unknown"
    data_lineage: tuple[str, ...] = ()
    score: float = 0.0
    risk_eligible: bool = True
    news_confirmed: bool = True
    evidence_retracted: bool = False
    correlation_cluster: str = "uncategorized"

    def __post_init__(self) -> None:
        if self.direction not in {"LONG", "SHORT", "NO_TRADE"}:
            raise ValueError("direction must be LONG, SHORT, or NO_TRADE")
        if not 0 <= self.calibrated_probability <= 1:
            raise ValueError("calibrated probability must be in [0,1]")
        if not 0 <= self.exit_feasibility_probability <= 1:
            raise ValueError("exit feasibility probability must be in [0,1]")
        if not self.horizon or self.decision_at_ms <= 0:
            raise ValueError("explicit horizon and decision time are required")
        if len(self.entry_zone) != 2 or min(self.entry_zone) <= 0:
            raise ValueError("entry zone must contain two positive prices")
        if self.entry_zone[0] > self.entry_zone[1]:
            raise ValueError("entry zone must be ordered")
        if self.invalidation <= 0 or any(target <= 0 for target in self.targets):
            raise ValueError("invalidation and targets must be positive")
        if self.liquidity_usd < 0 or self.data_freshness_seconds < 0:
            raise ValueError("liquidity and data freshness cannot be negative")
        if not 0 <= self.source_credibility <= 1:
            raise ValueError("source credibility must be in [0,1]")
        if min(self.spread_bps, self.estimated_slippage_bps, self.venue_divergence_bps) < 0:
            raise ValueError("market cost and divergence inputs cannot be negative")
        if self.segment == "new_memecoin" and not (self.chain_id and self.contract_address):
            raise ValueError("new/memecoin identity requires chain plus contract/mint")

    @classmethod
    def from_ranked(
        cls,
        candidate: Any,
        *,
        direction: str,
        canonical_asset_id: str,
        entry_zone: tuple[float, float],
        invalidation: float,
        targets: tuple[float, ...],
        spread_bps: float,
        estimated_slippage_bps: float,
        venue_divergence_bps: float,
        scenarios: tuple[str, ...],
        model_name: str,
        model_version: str,
        data_lineage: tuple[str, ...],
        supporting_evidence: tuple[Evidence, ...] = (),
        contradicting_evidence: tuple[Evidence, ...] = (),
        **identity_and_context: Any,
    ) -> "PaperOpportunity":
        """Losslessly bridge a frozen Phase 4 ranked candidate into Phase 5."""
        return cls(
            opportunity_id=str(candidate.opportunity_id),
            canonical_asset_id=canonical_asset_id,
            instrument_id=str(candidate.instrument_id), segment=str(candidate.segment),
            direction=direction, decision_at_ms=int(candidate.decision_at_ms),
            horizon=str(candidate.horizon), calibrated_probability=float(candidate.tp_probability),
            expected_net_move=float(candidate.expected_net_return), entry_zone=entry_zone,
            invalidation=invalidation, scenarios=scenarios, targets=targets,
            spread_bps=spread_bps, liquidity_usd=float(candidate.liquidity_usd),
            estimated_slippage_bps=estimated_slippage_bps,
            venue_divergence_bps=venue_divergence_bps,
            predicted_volatility=float(candidate.predicted_volatility),
            predicted_mae=float(candidate.predicted_mae),
            exit_feasibility_probability=float(candidate.exit_feasibility_probability),
            supporting_evidence=supporting_evidence,
            contradicting_evidence=contradicting_evidence,
            model_name=model_name, model_version=model_version, data_lineage=data_lineage,
            risk_eligible=bool(candidate.risk_eligible),
            **identity_and_context,
        )


@dataclass(frozen=True)
class RiskContext:
    paper_equity_usd: float
    current_gross_exposure_usd: float = 0.0
    open_position_count: int = 0
    cluster_exposure_usd: Mapping[str, float] = field(default_factory=dict)
    last_decision_at_ms: Mapping[str, int] = field(default_factory=dict)
    daily_net_pnl_usd: float = 0.0
    feed_stale: bool = False
    provider_disagreement: bool = False
    primary_venue_outage: bool = False
    model_drift: bool = False
    liquidity_loss: bool = False
    extreme_gap: bool = False
    now_ms: int = 0


@dataclass(frozen=True)
class RiskDecision:
    eligible: bool
    reasons: tuple[str, ...]
    paper_risk_usd: float
    paper_notional_usd: float


@dataclass(frozen=True)
class Recommendation:
    decision_id: str
    opportunity_id: str
    canonical_asset_id: str
    instrument_id: str
    chain_id: str | None
    contract_address: str | None
    venue: str | None
    decision_at_ms: int
    age_seconds: float
    ttl_seconds: int
    direction: str
    calibrated_probability: float
    expected_net_move: float
    entry_zone: tuple[float, float]
    invalidation: float
    scenarios: tuple[str, ...]
    targets: tuple[float, ...]
    horizon: str
    spread_bps: float
    liquidity_usd: float
    estimated_slippage_bps: float
    venue_divergence_bps: float
    supporting_evidence: tuple[Evidence, ...]
    contradicting_evidence: tuple[Evidence, ...]
    alternative_scenario: str
    source_credibility: float
    data_freshness_seconds: float
    model_name: str
    model_version: str
    data_lineage: tuple[str, ...]
    score: float
    maximum_paper_risk_usd: float
    paper_notional_usd: float
    segment: str
    correlation_cluster: str
    status: str
    risk_reasons: tuple[str, ...]
    shadow_mode: bool = True
    disclaimer: str = "Shadow paper recommendation only; not personalized financial advice."

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
