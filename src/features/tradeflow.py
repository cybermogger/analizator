from __future__ import annotations

from typing import Any, Iterable

from src.data.aggregation import aggregate_trades
from src.hyperliquid.schemas import Trade


def tradeflow_features(trades: Iterable[Trade], now_ms: int | None = None) -> dict[str, Any]:
    short = aggregate_trades(trades, 60_000, now_ms)
    medium = aggregate_trades(trades, 300_000, now_ms)
    if not medium.get("available"):
        return {"available": False, "quality": 0.0}
    result = dict(medium)
    result["delta_1m"] = short.get("delta", 0.0)
    result["delta_5m"] = medium.get("delta", 0.0)
    denom = abs(medium.get("delta", 0.0)) + 1e-12
    result["delta_acceleration"] = short.get("delta", 0.0) / denom
    result["large_trade_imbalance"] = (
        medium.get("large_buy_volume", 0.0) - medium.get("large_sell_volume", 0.0)
    ) / (medium.get("large_buy_volume", 0.0) + medium.get("large_sell_volume", 0.0) + 1e-12)
    result["quality"] = min(1.0, medium.get("trade_count", 0) / 100.0)
    return result
