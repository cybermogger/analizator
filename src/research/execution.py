from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


REQUIRED_COST_FIELDS = (
    "entry_fee_bps", "exit_fee_bps", "round_trip_spread_bps",
    "round_trip_slippage_bps", "latency_bps", "funding_bps", "impact_bps",
    "maximum_liquidity_participation", "minimum_fill_fraction",
)


@dataclass(frozen=True)
class ExecutionAssumptions:
    entry_fee_bps: float = 4.0
    exit_fee_bps: float = 4.0
    round_trip_spread_bps: float = 4.0
    round_trip_slippage_bps: float = 8.0
    latency_bps: float = 2.0
    funding_bps: float = 0.0
    impact_bps: float = 2.0
    maximum_liquidity_participation: float = 0.05
    minimum_fill_fraction: float = 0.25

    def __post_init__(self) -> None:
        nonnegative = (
            self.entry_fee_bps, self.exit_fee_bps, self.round_trip_spread_bps,
            self.round_trip_slippage_bps, self.latency_bps, self.impact_bps,
        )
        if any(value < 0 for value in nonnegative):
            raise ValueError("execution costs cannot be negative")
        if not 0 < self.maximum_liquidity_participation <= 1:
            raise ValueError("maximum_liquidity_participation must be in (0,1]")
        if not 0 < self.minimum_fill_fraction <= 1:
            raise ValueError("minimum_fill_fraction must be in (0,1]")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, float],
        *,
        require_complete: bool = True,
    ) -> "ExecutionAssumptions":
        missing = [name for name in REQUIRED_COST_FIELDS if name not in values]
        if require_complete and missing:
            raise ValueError("execution cost model is incomplete: " + ", ".join(missing))
        allowed = {name: float(values[name]) for name in REQUIRED_COST_FIELDS if name in values}
        return cls(**allowed)


@dataclass(frozen=True)
class TradeOpportunity:
    opportunity_id: str
    direction: int
    requested_notional_usd: float
    entry_price: float
    exit_price: float
    entry_liquidity_usd: float
    exit_liquidity_usd: float

    def __post_init__(self) -> None:
        if self.direction not in (-1, 1):
            raise ValueError("direction must be -1 or 1")
        if self.requested_notional_usd <= 0 or self.entry_price <= 0 or self.exit_price <= 0:
            raise ValueError("notional and prices must be positive")


@dataclass(frozen=True)
class ExecutionResult:
    opportunity_id: str
    status: str
    requested_notional_usd: float
    filled_notional_usd: float
    fill_fraction: float
    gross_pnl_usd: float
    fees_usd: float
    spread_usd: float
    slippage_usd: float
    latency_usd: float
    funding_usd: float
    impact_usd: float
    net_pnl_usd: float
    net_return: float

    def reconciles(self, tolerance: float = 1e-9) -> bool:
        expected = self.gross_pnl_usd - self.fees_usd - self.spread_usd \
            - self.slippage_usd - self.latency_usd - self.funding_usd - self.impact_usd
        return abs(expected - self.net_pnl_usd) <= tolerance


def simulate_execution(
    opportunity: TradeOpportunity,
    assumptions: ExecutionAssumptions,
) -> ExecutionResult:
    entry_capacity = (
        opportunity.entry_liquidity_usd * assumptions.maximum_liquidity_participation
    )
    # Exit liquidity is quoted at the exit price. Convert it back to entry
    # notional so partial-fill accounting uses one consistent quantity.
    exit_capacity = (
        opportunity.exit_liquidity_usd
        * assumptions.maximum_liquidity_participation
        * opportunity.entry_price / opportunity.exit_price
    )
    capacity = min(entry_capacity, exit_capacity)
    filled = min(opportunity.requested_notional_usd, max(capacity, 0.0))
    fill_fraction = filled / opportunity.requested_notional_usd
    if fill_fraction < assumptions.minimum_fill_fraction:
        return ExecutionResult(
            opportunity.opportunity_id, "REJECTED_LIQUIDITY", opportunity.requested_notional_usd,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        )
    exit_notional = filled * opportunity.exit_price / opportunity.entry_price
    gross_return = opportunity.direction * (opportunity.exit_price / opportunity.entry_price - 1.0)
    gross = filled * gross_return
    fees = (
        filled * assumptions.entry_fee_bps
        + exit_notional * assumptions.exit_fee_bps
    ) / 10_000.0
    spread = filled * assumptions.round_trip_spread_bps / 10_000.0
    slippage = filled * assumptions.round_trip_slippage_bps / 10_000.0
    latency = filled * assumptions.latency_bps / 10_000.0
    impact = filled * assumptions.impact_bps / 10_000.0
    # Positive funding is paid by longs and received by shorts; it may therefore
    # be negative as a cost, which is required for exact accounting.
    funding = filled * assumptions.funding_bps * opportunity.direction / 10_000.0
    net = gross - fees - spread - slippage - latency - funding - impact
    result = ExecutionResult(
        opportunity.opportunity_id,
        "FILLED" if fill_fraction == 1.0 else "PARTIAL",
        opportunity.requested_notional_usd,
        filled,
        fill_fraction,
        gross,
        fees,
        spread,
        slippage,
        latency,
        funding,
        impact,
        net,
        net / filled,
    )
    if not result.reconciles():
        raise AssertionError("execution accounting does not reconcile")
    return result
