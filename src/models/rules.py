from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ComponentProbability:
    probabilities: tuple[float, float, float]
    quality: float
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = np.asarray(self.probabilities, dtype=float)
        if not np.all(np.isfinite(values)) or np.any(values < -1e-9) or np.any(values > 1 + 1e-9) or not np.isclose(values.sum(), 1.0, atol=1e-8):
            raise ValueError(f"Invalid probabilities: {self.probabilities}")


def directional_distribution(score: float, strength: float = 1.0, quality: float = 1.0) -> tuple[float, float, float]:
    """Conservative logistic opinion; this is an ensemble component, not a claimed forecast calibration."""
    score = float(np.clip(score, -3, 3)) * float(np.clip(strength, 0, 2))
    logits = np.array([score, -score, 0.8 - abs(score) * 0.35])
    logits -= logits.max()
    raw = np.exp(logits)
    raw /= raw.sum()
    prior = np.array([0.24, 0.24, 0.52])
    q = float(np.clip(quality, 0, 1))
    mixed = q * raw + (1 - q) * prior
    return tuple((mixed / mixed.sum()).tolist())


def technical_component(frames: dict[str, pd.DataFrame], weights: dict[str, float]) -> ComponentProbability:
    total, used, reasons = 0.0, 0.0, []
    ordered_scores: dict[str, float] = {}
    for interval, base_weight in weights.items():
        frame = frames.get(interval)
        if frame is None or frame.empty:
            continue
        row = frame.iloc[-1]
        close = float(row["close"])
        trend = np.mean([
            np.sign(close - float(row.get("ema_21", close))),
            np.sign(float(row.get("ema_21", close)) - float(row.get("ema_50", close))),
            np.sign(float(row.get("ema_50", close)) - float(row.get("ema_200", close))),
            np.sign(float(row.get("plus_di", 0)) - float(row.get("minus_di", 0))),
        ])
        rsi_value = float(row.get("rsi_14", 50))
        momentum = np.mean([
            np.tanh((rsi_value - 50) / 15),
            np.sign(float(row.get("macd_hist", 0))),
            np.tanh(float(row.get("roc_10", 0)) / (float(row.get("atr_pct", 0.01)) * 3 + 1e-12)),
        ])
        location = np.mean([
            np.tanh(float(row.get("vwap_distance", 0)) / (float(row.get("atr_pct", 0.01)) + 1e-12)),
            np.clip((float(row.get("bb_position", 0.5)) - 0.5) * 2, -1, 1),
        ])
        volume_confirmation = np.clip(float(row.get("relative_volume", 1)) - 1, -0.5, 0.5)
        score = float(0.48 * trend + 0.28 * momentum + 0.16 * location + 0.08 * np.sign(score_or_zero(momentum)) * volume_confirmation)
        ordered_scores[interval] = score
        weight = float(base_weight)
        if interval == "1m" and "1h" in ordered_scores and np.sign(score) != np.sign(ordered_scores["1h"]):
            weight *= 0.35
        total += weight * score
        used += weight
    score = total / used if used else 0.0
    if ordered_scores:
        bullish = sum(1 for x in ordered_scores.values() if x > 0.18)
        bearish = sum(1 for x in ordered_scores.values() if x < -0.18)
        if bullish >= 3:
            reasons.append(f"{bullish} таймфрейма подтверждают bullish trend/momentum")
        if bearish >= 3:
            reasons.append(f"{bearish} таймфрейма подтверждают bearish trend/momentum")
    quality = min(1.0, used / max(sum(weights.values()), 1e-12))
    return ComponentProbability(directional_distribution(score, 1.4, quality), quality, tuple(reasons))


def score_or_zero(value: Any) -> float:
    try:
        value = float(value)
        return value if np.isfinite(value) else 0.0
    except (TypeError, ValueError):
        return 0.0


def structure_component(frames: dict[str, pd.DataFrame], weights: dict[str, float]) -> ComponentProbability:
    score, used, reasons = 0.0, 0.0, []
    for interval, weight in weights.items():
        frame = frames.get(interval)
        if frame is None or frame.empty:
            continue
        row = frame.iloc[-1]
        recent = frame.iloc[-20:]
        local = score_or_zero(row.get("structure_score", 0))
        local += 0.8 * (score_or_zero(row.get("bull_bos", 0)) - score_or_zero(row.get("bear_bos", 0)))
        local += 0.9 * (score_or_zero(row.get("bull_choch", 0)) - score_or_zero(row.get("bear_choch", 0)))
        local += 0.5 * (score_or_zero(row.get("retest_up", 0)) - score_or_zero(row.get("retest_down", 0)))
        score += weight * np.tanh(local)
        used += weight
        bull_structure = int(recent.get("hh", pd.Series(dtype=float)).sum()) + int(recent.get("hl", pd.Series(dtype=float)).sum())
        bear_structure = int(recent.get("lh", pd.Series(dtype=float)).sum()) + int(recent.get("ll", pd.Series(dtype=float)).sum())
        if bull_structure > bear_structure:
            reasons.append(f"{interval} структура HH/HL")
        elif bear_structure > bull_structure:
            reasons.append(f"{interval} структура LH/LL")
        if row.get("bull_bos", 0): reasons.append(f"{interval} bullish BOS")
        if row.get("bear_bos", 0): reasons.append(f"{interval} bearish BOS")
    result = score / used if used else 0.0
    quality = min(1.0, used / max(sum(weights.values()), 1e-12))
    return ComponentProbability(directional_distribution(result, 1.5, quality), quality, tuple(dict.fromkeys(reasons)))


def derivatives_component(values: dict[str, Any]) -> ComponentProbability:
    if not values.get("available"):
        return ComponentProbability((0.24, 0.24, 0.52), 0.0)
    score, reasons = 0.0, []
    mapping = values.get("price_oi_combinations", {})
    horizon_weights = {"5m": 0.15, "15m": 0.25, "30m": 0.2, "1h": 0.25, "4h": 0.15}
    for window, weight in horizon_weights.items():
        state = mapping.get(window, "UNKNOWN")
        if state == "NEW_LONG_POSITIONING_PROXY": score += weight
        elif state == "NEW_SHORT_POSITIONING_PROXY": score -= weight
        elif state == "SHORT_COVERING_PROXY": score += weight * 0.45
        elif state == "LONG_LIQUIDATION_PROXY": score -= weight * 0.45
    funding = score_or_zero(values.get("funding"))
    premium = score_or_zero(values.get("mark_oracle_premium"))
    score -= np.tanh(funding * 10_000) * 0.1
    score -= np.tanh(premium * 300) * 0.1
    oi_1h = values.get("oi_change_1h")
    if oi_1h is not None:
        reasons.append(f"OI 1h {oi_1h:+.1%}")
    quality = float(values.get("quality", 0))
    return ComponentProbability(directional_distribution(score, 1.3, quality), quality, tuple(reasons))


def orderbook_component(values: dict[str, Any]) -> ComponentProbability:
    if not values.get("available"):
        return ComponentProbability((0.24, 0.24, 0.52), 0.0)
    score = 0.55 * score_or_zero(values.get("weighted_imbalance")) + 0.25 * score_or_zero(values.get("imbalance")) + 0.2 * np.tanh(score_or_zero(values.get("book_pressure")))
    reason = f"order book {'bid' if score >= 0 else 'ask'} imbalance {abs(score_or_zero(values.get('weighted_imbalance'))):.0%}"
    return ComponentProbability(directional_distribution(score, 2.2, 1.0), 1.0, (reason,))


def tradeflow_component(values: dict[str, Any]) -> ComponentProbability:
    if not values.get("available"):
        return ComponentProbability((0.24, 0.24, 0.52), 0.0)
    score = 0.65 * score_or_zero(values.get("aggressive_imbalance")) + 0.35 * score_or_zero(values.get("large_trade_imbalance"))
    quality = float(values.get("quality", 0))
    reason = "aggressive buy trade flow" if score >= 0 else "aggressive sell trade flow"
    return ComponentProbability(directional_distribution(score, 2.0, quality), quality, (reason,))


def btc_component(values: dict[str, Any]) -> ComponentProbability:
    if not values.get("available"):
        return ComponentProbability((0.24, 0.24, 0.52), 0.0)
    score = score_or_zero(values.get("score"))
    quality = float(values.get("quality", 0))
    reason = f"BTC Hyperliquid context {'bullish' if score >= 0 else 'bearish'}"
    return ComponentProbability(directional_distribution(score, 0.8, quality), quality, (reason,))
