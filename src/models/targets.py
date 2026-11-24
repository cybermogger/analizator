from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


LONG, SHORT, WAIT = 0, 1, 2


def dynamic_barriers(
    frame: pd.DataFrame,
    atr_multiplier: float = 1.25,
    horizon_steps: int = 1,
    volatility_multiplier: float = 0.0,
    mode: str = "atr",
    use_structure: bool = True,
) -> tuple[pd.Series, pd.Series]:
    """Causal, state-dependent barrier levels known at the prediction timestamp."""
    if atr_multiplier <= 0 or volatility_multiplier < 0 or horizon_steps < 1:
        raise ValueError("Invalid dynamic barrier parameters")
    if mode not in {"atr", "max", "blend"}:
        raise ValueError(f"Unsupported barrier mode: {mode}")
    close = frame["close"]
    atr_value = frame["atr_14"].clip(lower=close * 0.001)
    atr_width = atr_value * atr_multiplier
    # Rolling sigma is causal. Scaling by sqrt(horizon) makes 15m/1h/4h
    # targets comparable without using a fixed percentage threshold.
    log_returns = np.log(close).diff()
    sigma = log_returns.rolling(48, min_periods=20).std(ddof=0)
    volatility_width = close * sigma * np.sqrt(horizon_steps) * volatility_multiplier
    volatility_width = volatility_width.fillna(atr_width)
    if mode == "atr" or volatility_multiplier == 0:
        base = atr_width
    elif mode == "max":
        base = pd.concat([atr_width, volatility_width], axis=1).max(axis=1)
    else:
        base = 0.5 * (atr_width + volatility_width)
    base = base.clip(lower=close * 0.001, upper=close * 0.20)
    upper_distance, lower_distance = base.copy(), base.copy()
    if use_structure:
        resistance_distance = (frame.get("resistance", close + base) - close).where(lambda x: x > 0)
        support_distance = (close - frame.get("support", close - base)).where(lambda x: x > 0)
        structural_upper = resistance_distance.clip(lower=base * 0.7, upper=base * 2.5)
        structural_lower = support_distance.clip(lower=base * 0.7, upper=base * 2.5)
        upper_distance = pd.concat([base, structural_upper], axis=1).min(axis=1)
        lower_distance = pd.concat([base, structural_lower], axis=1).min(axis=1)
    return close + upper_distance, close - lower_distance


def triple_barrier_labels(
    frame: pd.DataFrame,
    horizon_steps: int,
    atr_multiplier: float = 1.25,
    *,
    volatility_multiplier: float = 0.0,
    barrier_mode: str = "atr",
    use_structure: bool = True,
) -> pd.Series:
    """Targets may inspect future bars; input feature columns are never modified."""
    if horizon_steps < 1:
        raise ValueError("horizon_steps must be positive")
    upper, lower = dynamic_barriers(
        frame, atr_multiplier, horizon_steps, volatility_multiplier, barrier_mode, use_structure,
    )
    labels = pd.Series(np.nan, index=frame.index, dtype=float)
    # Use array indexing inside the horizon loop. Repeated DataFrame/Series
    # lookups here otherwise pause live scanning when many assets refresh.
    upper_values, lower_values = upper.to_numpy(), lower.to_numpy()
    high_values, low_values = frame['high'].to_numpy(), frame['low'].to_numpy()
    label_values = labels.to_numpy()
    for position in range(0, len(frame) - horizon_steps):
        up, down = float(upper_values[position]), float(lower_values[position])
        label = WAIT
        for future_position in range(position + 1, position + horizon_steps + 1):
            hits_up = float(high_values[future_position]) >= up
            hits_down = float(low_values[future_position]) <= down
            if hits_up and hits_down:
                label = WAIT  # Intrabar ordering is unknowable from OHLC; never invent it.
                break
            if hits_up:
                label = LONG
                break
            if hits_down:
                label = SHORT
                break
        label_values[position] = label
    return labels


def calculate_trade_plan(mark: float, direction: str, row: Any, analogue_move: float | None = None) -> dict[str, Any]:
    atr_value = max(float(row.get("atr_14", mark * 0.005) or mark * 0.005), mark * 0.001)
    support = float(row.get("support", mark - atr_value * 1.5) or mark - atr_value * 1.5)
    resistance = float(row.get("resistance", mark + atr_value * 1.5) or mark + atr_value * 1.5)
    expected = max(abs(analogue_move or 0) * mark, atr_value * 1.2)
    if direction == "LONG":
        tp1 = min(mark + atr_value, resistance) if resistance > mark else mark + atr_value
        tp2 = max(tp1, min(mark + expected * 1.6, mark + atr_value * 2.2))
        invalidation = min(mark - atr_value, support) if support < mark else mark - atr_value
        zone = (mark - atr_value * 0.35, mark + atr_value * 0.1)
        expected_zone = (tp1, tp2)
    elif direction == "SHORT":
        tp1 = max(mark - atr_value, support) if support < mark else mark - atr_value
        tp2 = min(tp1, max(mark - expected * 1.6, mark - atr_value * 2.2))
        invalidation = max(mark + atr_value, resistance) if resistance > mark else mark + atr_value
        zone = (mark - atr_value * 0.1, mark + atr_value * 0.35)
        expected_zone = (tp2, tp1)
    else:
        return {"entry_zone": None, "tp1": None, "tp2": None, "invalidation": None, "expected_zone": (mark - atr_value, mark + atr_value), "expected_move_pct": 0.0, "risk_reward": None, "action": "WAIT"}
    reward = abs(tp2 - mark)
    risk = abs(invalidation - mark)
    stretched = abs(float(row.get("vwap_distance", 0) or 0)) > max(float(row.get("atr_pct", 0.01) or 0.01) * 1.5, 0.01)
    action = ("LONG ON RETEST" if direction == "LONG" else "SHORT ON RETEST") if stretched else ("LONG NOW" if direction == "LONG" else "SHORT NOW")
    if stretched and abs(float(row.get("impulse", 0) or 0)) > 1.5:
        action = "DO NOT CHASE"
    return {
        "entry_zone": tuple(sorted(zone)), "tp1": tp1, "tp2": tp2, "invalidation": invalidation,
        "expected_zone": tuple(sorted(expected_zone)), "expected_move_pct": (tp2 / mark - 1) * 100,
        "risk_reward": reward / (risk + 1e-12), "action": action,
    }
