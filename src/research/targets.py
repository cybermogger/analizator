from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .types import ForwardOutcome


HORIZONS_MS = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "24h": 24 * 60 * 60_000,
}


@dataclass(frozen=True)
class PricePoint:
    timestamp_ms: int
    close: float
    high: float
    low: float
    exit_notional_usd: float | None = None


def forward_outcomes(
    *,
    decision_at_ms: int,
    entry_price: float,
    path: Iterable[PricePoint],
    round_trip_cost_bps: float,
    take_profit: float,
    stop_loss: float,
    required_exit_notional_usd: float,
    entry_liquidity_usd: float | None = None,
    direction: int = 1,
) -> tuple[ForwardOutcome, ...]:
    """Create labels from future observations; callers must never expose them as features."""

    future = sorted((point for point in path if point.timestamp_ms > decision_at_ms),
                    key=lambda point: point.timestamp_ms)
    outcomes: list[ForwardOutcome] = []
    for name, horizon_ms in HORIZONS_MS.items():
        end = decision_at_ms + horizon_ms
        exit_point = next((point for point in future if point.timestamp_ms >= end), None)
        if exit_point is None:
            continue
        window = [point for point in future if point.timestamp_ms <= exit_point.timestamp_ms]
        returns = [(point.close / entry_price) - 1.0 for point in window]
        high_return = max((point.high / entry_price) - 1.0 for point in window)
        low_return = min((point.low / entry_price) - 1.0 for point in window)
        tp_time = next((point.timestamp_ms for point in window
                        if point.high >= entry_price * (1.0 + take_profit)), None)
        stop_time = next((point.timestamp_ms for point in window
                          if point.low <= entry_price * (1.0 - stop_loss)), None)
        tp_before_stop = None if tp_time is None and stop_time is None else (
            tp_time is not None and (stop_time is None or tp_time < stop_time)
        )
        mean = sum(returns) / len(returns)
        realized_volatility = math.sqrt(
            sum((value - mean) ** 2 for value in returns) / max(len(returns) - 1, 1)
        )
        exit_capacity = window[-1].exit_notional_usd
        outcomes.append(ForwardOutcome(
            horizon=name,
            horizon_ms=horizon_ms,
            label_end_at_ms=exit_point.timestamp_ms,
            gross_return=returns[-1],
            net_return=returns[-1] - round_trip_cost_bps / 10_000.0,
            tp_before_stop=tp_before_stop,
            mae=low_return,
            mfe=high_return,
            realized_volatility=realized_volatility,
            exit_feasible=exit_capacity is not None and exit_capacity >= required_exit_notional_usd,
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_point.close,
            entry_liquidity_usd=entry_liquidity_usd,
            exit_liquidity_usd=exit_capacity,
        ))
    return tuple(outcomes)
