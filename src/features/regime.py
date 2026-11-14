from __future__ import annotations

from enum import StrEnum
from typing import Any

import numpy as np


class MarketRegime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    REVERSAL_RISK = "REVERSAL_RISK"


def detect_regime(row: Any, book: dict[str, Any] | None = None, derivatives: dict[str, Any] | None = None) -> MarketRegime:
    adx = float(row.get("adx", 0) or 0)
    atr_pct = float(row.get("atr_pct", 0) or 0)
    median_vol = float(row.get("rolling_std_20", atr_pct) or atr_pct)
    ema21, ema50, close = float(row.get("ema_21", row["close"])), float(row.get("ema_50", row["close"])), float(row["close"])
    if int(row.get("bull_choch", 0) or row.get("bear_choch", 0)) or int(row.get("trend_exhaustion", 0)):
        return MarketRegime.REVERSAL_RISK
    if int(row.get("breakout_up", 0) or row.get("breakout_down", 0)) and float(row.get("relative_volume", 0) or 0) > 1.2:
        return MarketRegime.BREAKOUT
    if atr_pct > max(0.02, median_vol * 2.0):
        return MarketRegime.HIGH_VOLATILITY
    if atr_pct < 0.002 and float(row.get("bb_width", 1) or 1) < 0.02:
        return MarketRegime.LOW_VOLATILITY
    if adx >= 23 and ema21 > ema50 and close > ema21:
        return MarketRegime.TREND_UP
    if adx >= 23 and ema21 < ema50 and close < ema21:
        return MarketRegime.TREND_DOWN
    return MarketRegime.RANGE


def regime_component_multipliers(regime: MarketRegime, horizon: str) -> dict[str, float]:
    values = {"technical": 1.0, "structure": 1.0, "derivatives": 1.0, "orderbook": 1.0, "tradeflow": 1.0, "statistical": 1.0, "ml": 1.0, "btc_context": 1.0}
    if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
        values.update(technical=1.35, structure=1.25, statistical=0.85)
    elif regime == MarketRegime.RANGE:
        values.update(technical=0.9, structure=1.25, statistical=1.3, orderbook=1.1)
    elif regime == MarketRegime.BREAKOUT:
        values.update(structure=1.3, derivatives=1.3, orderbook=1.35, tradeflow=1.35)
    elif regime == MarketRegime.REVERSAL_RISK:
        values.update(structure=1.35, statistical=1.2, technical=0.8)
    if horizon == "15m":
        values["orderbook"] *= 1.3
        values["tradeflow"] *= 1.3
    elif horizon == "4h":
        values["orderbook"] *= 0.55
        values["tradeflow"] *= 0.55
        values["btc_context"] *= 1.3
    return values
