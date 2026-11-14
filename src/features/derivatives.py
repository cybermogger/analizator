from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.hyperliquid.schemas import AssetContext


WINDOWS_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}


def _change_at(history: pd.DataFrame, column: str, timestamp: int, window_ms: int, current: float) -> float | None:
    if history.empty or column not in history:
        return None
    earlier = history[history["timestamp"] <= timestamp - window_ms]
    if earlier.empty:
        return None
    previous = float(earlier.iloc[-1][column])
    return (current / previous - 1.0) if previous != 0 else None


def _delta_at(history: pd.DataFrame, column: str, timestamp: int, window_ms: int, current: float) -> float | None:
    if history.empty or column not in history:
        return None
    earlier = history[history["timestamp"] <= timestamp - window_ms]
    if earlier.empty:
        return None
    return current - float(earlier.iloc[-1][column])


def derivatives_features(context: AssetContext, history: pd.DataFrame, price_changes: dict[str, float | None] | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "available": True, "mark": context.mark, "oracle": context.oracle,
        "mark_oracle_premium": context.mark / context.oracle - 1 if context.oracle else None,
        "funding": context.funding, "open_interest": context.open_interest,
        "day_notional_volume": context.day_notional_volume,
    }
    combinations: dict[str, str] = {}
    for label, milliseconds in WINDOWS_MS.items():
        oi_change = _change_at(history, "open_interest", context.timestamp, milliseconds, context.open_interest)
        funding_change = _delta_at(history, "funding", context.timestamp, milliseconds, context.funding)
        premium_now = context.premium if context.premium is not None else 0.0
        premium_change = _delta_at(history, "premium", context.timestamp, milliseconds, premium_now)
        values[f"oi_change_{label}"] = oi_change
        values[f"funding_change_{label}"] = funding_change
        values[f"premium_change_{label}"] = premium_change
        price_change = (price_changes or {}).get(label)
        if price_change is None or oi_change is None:
            combinations[label] = "UNKNOWN"
        elif price_change >= 0 and oi_change >= 0:
            combinations[label] = "NEW_LONG_POSITIONING_PROXY"
        elif price_change < 0 and oi_change >= 0:
            combinations[label] = "NEW_SHORT_POSITIONING_PROXY"
        elif price_change >= 0 and oi_change < 0:
            combinations[label] = "SHORT_COVERING_PROXY"
        else:
            combinations[label] = "LONG_LIQUIDATION_PROXY"
    values["price_oi_combinations"] = combinations
    known = [values[f"oi_change_{x}"] for x in WINDOWS_MS if values[f"oi_change_{x}"] is not None]
    values["quality"] = min(1.0, len(known) / len(WINDOWS_MS))
    return values
