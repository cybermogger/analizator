from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np


PROBABILITY_KEYS = ("LONG", "SHORT", "WAIT")
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}


def normalized_percentages(values: Mapping[str, float]) -> dict[str, float]:
    if set(values) != set(PROBABILITY_KEYS):
        raise ValueError("Probability map must contain exactly LONG, SHORT, WAIT")
    raw = np.asarray([values[key] for key in PROBABILITY_KEYS], dtype=float)
    if not np.all(np.isfinite(raw)) or np.any(raw < 0):
        raise ValueError(f"Invalid probabilities: {values}")
    total = float(raw.sum())
    if total <= 0:
        raise ValueError("Probability sum must be positive")
    raw = np.clip(raw / total * 100.0, 0.0, 100.0)
    raw[2] = 100.0 - raw[0] - raw[1]
    if np.any(raw < -1e-8) or not np.isclose(raw.sum(), 100.0, atol=1e-8):
        raise ValueError(f"Probability normalization failed: {raw.tolist()}")
    return {key: float(raw[index]) for index, key in enumerate(PROBABILITY_KEYS)}


def shift_overconfidence_to_wait(values: Mapping[str, float], scale: float) -> dict[str, float]:
    result = normalized_percentages(values)
    scale = float(np.clip(scale, 0.5, 1.0))
    index = "LONG" if result["LONG"] >= result["SHORT"] else "SHORT"
    transferred = result[index] * (1.0 - scale)
    result[index] -= transferred
    result["WAIT"] += transferred
    return normalized_percentages(result)


def _finite(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else fallback
    except (TypeError, ValueError):
        return fallback


def market_events(
    row: Any,
    regime: str,
    tradeflow: Mapping[str, Any],
    btc_context: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> list[str]:
    events: list[str] = []
    if int(_finite(row.get("breakout_up", 0))):
        events.append("BREAKOUT")
    if int(_finite(row.get("breakout_down", 0))):
        events.append("BREAKDOWN")
    range_expansion = _finite(row.get("range_expansion", 1), 1)
    if regime == "HIGH_VOLATILITY" or range_expansion >= float(settings.get("volatility_spike_ratio", 2.0)):
        events.append("VOLATILITY_SPIKE")
    flow = _finite(tradeflow.get("aggressive_imbalance"))
    if tradeflow.get("available") and abs(flow) >= float(settings.get("fast_flow_imbalance", 0.65)):
        events.append("ORDER_FLOW_IMBALANCE")
    btc_momentum = [_finite(value) for value in btc_context.get("momentum", {}).values()]
    btc_shock = max((abs(value) for value in btc_momentum), default=0.0)
    if btc_context.get("available") and btc_shock >= float(settings.get("btc_shock_return", 0.025)):
        events.append("BTC_SHOCK")
    return events


def enrich_signal_quality(
    *,
    mark: float,
    timestamp: int,
    context_timestamp: int,
    source_interval: str,
    source_row: Any,
    horizons: Mapping[str, Mapping[str, float]],
    primary_horizon: str,
    direction: str,
    details: Mapping[str, Any],
    plans: Mapping[str, Mapping[str, Any]],
    regime: str,
    orderbook: Mapping[str, Any],
    tradeflow: Mapping[str, Any],
    btc_context: Mapping[str, Any],
    settings: Mapping[str, Any],
    context_received_at_ms: int | None = None,
    context_exchange_timestamp_ms: int | None = None,
    freshness_checked_at_ms: int | None = None,
    confidence_scale: float = 1.0,
) -> dict[str, Any]:
    primary = normalized_percentages(horizons[primary_horizon])
    directional = "LONG" if primary["LONG"] >= primary["SHORT"] else "SHORT"
    directional_probability = primary[directional]
    gap = abs(primary["LONG"] - primary["SHORT"])

    winners = [max(PROBABILITY_KEYS, key=lambda key: values[key]) for values in horizons.values()]
    timeframe_agreement = sum(winner == directional for winner in winners) / max(len(winners), 1)
    component_values = details[primary_horizon].get("components", {})
    component_votes = []
    component_quality = []
    for value in component_values.values():
        quality = float(np.clip(_finite(value.get("quality")), 0.0, 1.0))
        if float(value.get("effective_weight", 1.0)) <= 0:
            continue
        component_quality.append(quality)
        probabilities = value.get("probabilities", {})
        if quality >= 0.2 and probabilities:
            component_votes.append(max(PROBABILITY_KEYS, key=lambda key: probabilities[key]))
    component_agreement = (
        sum(vote == directional for vote in component_votes) / len(component_votes)
        if component_votes else 0.0
    )
    coverage = float(np.mean(component_quality)) if component_quality else 0.0

    freshness_now = int(freshness_checked_at_ms or timestamp)

    def receive_age(value: Any) -> float | None:
        received = int(_finite(value, 0))
        if received <= 0:
            return None
        return max(0.0, (freshness_now - received) / 1000)

    # Feed liveness is a transport property. Exchange timestamps remain useful
    # event time, but must not be treated as local receipt/heartbeat time.
    context_receive_age = receive_age(context_received_at_ms)
    candle_open = int(_finite(source_row.get("open_time"), timestamp))
    interval_ms = INTERVAL_MS.get(source_interval, 60_000)
    interval_seconds = interval_ms / 1000
    candle_receive_age = receive_age(source_row.get("received_at_ms"))
    current_interval_open = freshness_now - freshness_now % interval_ms
    interval_slack_ms = 5_000
    candle_interval_valid = (
        current_interval_open - interval_ms - interval_slack_ms
        <= candle_open
        <= current_interval_open + interval_slack_ms
    )
    book_receive_age = receive_age(orderbook.get("received_at_ms")) if orderbook.get("available") else None
    max_core_age = float(settings.get("max_data_age_seconds", 180))

    def receive_quality(age: float | None) -> float:
        if age is None:
            return 0.0
        return max(0.0, 1.0 - age / max(max_core_age, 1.0))

    fresh = bool(
        context_receive_age is not None
        and context_receive_age <= max_core_age
        and candle_receive_age is not None
        and candle_receive_age <= max_core_age
        and candle_interval_valid
        and book_receive_age is not None
        and book_receive_age <= max_core_age
    )
    freshness_quality = min(
        1.0,
        receive_quality(context_receive_age),
        receive_quality(candle_receive_age) if candle_interval_valid else 0.0,
        receive_quality(book_receive_age),
    )
    book_exchange_timestamp = int(_finite(orderbook.get("exchange_timestamp_ms", orderbook.get("timestamp")), 0))
    book_received_at = int(_finite(orderbook.get("received_at_ms"), 0))
    if book_exchange_timestamp > 0 and book_received_at > 0:
        exchange_clock_skew_seconds: float | None = (book_received_at - book_exchange_timestamp) / 1000
    elif context_exchange_timestamp_ms and context_received_at_ms:
        exchange_clock_skew_seconds = (int(context_received_at_ms) - int(context_exchange_timestamp_ms)) / 1000
    else:
        exchange_clock_skew_seconds = None

    contradiction = max(0.0, 0.5 - component_agreement) * 2.0
    confidence = (
        0.72 * gap
        + 0.20 * (100.0 - primary["WAIT"])
        + 18.0 * timeframe_agreement
        + 12.0 * component_agreement
        + 8.0 * coverage
        - 12.0 * contradiction
    )
    confidence *= freshness_quality * float(np.clip(confidence_scale, 0.5, 1.0))
    confidence = float(np.clip(confidence, 0.0, 100.0))
    signal_strength = (
        0.35 * directional_probability
        + 0.75 * gap
        + 16.0 * timeframe_agreement
        + 12.0 * component_agreement
        + 8.0 * coverage
        - 0.25 * primary["WAIT"]
        - 15.0 * contradiction
    )
    if direction == "WAIT" or not fresh:
        signal_strength *= 0.45
    signal_strength = float(np.clip(signal_strength, 0.0, 100.0))

    atr_value = max(_finite(source_row.get("atr_14"), mark * 0.005), mark * 0.001)
    rolling_std = max(0.0, _finite(source_row.get("rolling_std_20")))
    horizon_steps = int(settings.get("horizon_steps", 1))
    distribution = details[primary_horizon].get("forward_return_quantiles", {})
    distribution_width = max(
        abs(_finite(distribution.get("p10"))),
        abs(_finite(distribution.get("p90"))),
    ) * mark
    expected_distance = max(atr_value * 1.2, mark * rolling_std * math.sqrt(max(1, horizon_steps)), distribution_width)
    expected_range = (mark - expected_distance, mark + expected_distance)
    target_range = plans[direction].get("expected_zone") if direction in {"LONG", "SHORT"} else None
    if direction == "LONG" and target_range:
        expected_move = ((target_range[0] / mark - 1) * 100, (target_range[1] / mark - 1) * 100)
    elif direction == "SHORT" and target_range:
        expected_move = ((target_range[0] / mark - 1) * 100, (target_range[1] / mark - 1) * 100)
    else:
        expected_move = ((expected_range[0] / mark - 1) * 100, (expected_range[1] / mark - 1) * 100)
    support = _finite(source_row.get("support"), mark - atr_value * 1.5)
    resistance = _finite(source_row.get("resistance"), mark + atr_value * 1.5)
    volatility_pct = max(_finite(source_row.get("atr_pct")), rolling_std) * 100.0

    return {
        "confidence": confidence,
        "signal_strength": signal_strength,
        "directional_gap": gap,
        "timeframe_agreement": timeframe_agreement,
        "component_agreement": component_agreement,
        "independent_confirmations": sum(vote == directional for vote in component_votes),
        "expected_move": tuple(sorted(expected_move)),
        "expected_range": tuple(sorted(expected_range)),
        "target_range": tuple(sorted(target_range)) if target_range else None,
        "bullish_target": tuple(sorted(plans["LONG"].get("expected_zone", ()))) or None,
        "bearish_target": tuple(sorted(plans["SHORT"].get("expected_zone", ()))) or None,
        "invalidation": plans[direction].get("invalidation") if direction in {"LONG", "SHORT"} else None,
        "support": support,
        "resistance": resistance,
        "volatility": {"percent": volatility_pct, "atr": atr_value, "range_expansion": _finite(source_row.get("range_expansion"), 1)},
        "data_freshness": {
            "status": "FRESH" if fresh else "STALE",
            "age_seconds": max(
                age for age in (context_receive_age, book_receive_age, candle_receive_age)
                if age is not None
            ) if any(age is not None for age in (context_receive_age, book_receive_age, candle_receive_age)) else None,
            "source_candle_age_seconds": candle_receive_age,
            "quality": freshness_quality,
            "context_receive_age_seconds": context_receive_age,
            "book_receive_age_seconds": book_receive_age,
            "candle_receive_age_seconds": candle_receive_age,
            "candle_interval_valid": candle_interval_valid,
            "exchange_clock_skew_seconds": exchange_clock_skew_seconds,
        },
        "market_events": market_events(source_row, regime, tradeflow, btc_context, settings),
        "outcome_threshold_pct": max(atr_value / mark * 60.0, rolling_std * math.sqrt(max(1, horizon_steps)) * 100.0),
    }
