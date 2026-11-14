from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .statistics import rolling_relationship


def _finite(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else fallback
    except (TypeError, ValueError):
        return fallback


def btc_context_features(asset_frames: dict[str, pd.DataFrame], btc_frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    trends: dict[str, float] = {}
    momentum: dict[str, float] = {}
    volatility: dict[str, float] = {}
    relationships: dict[str, dict[str, float | None]] = {}
    for interval in ("5m", "15m", "1h", "4h"):
        btc = btc_frames.get(interval)
        if btc is None or btc.empty:
            continue
        row = btc.iloc[-1]
        close = _finite(row["close"])
        ema21, ema50 = _finite(row.get("ema_21"), close), _finite(row.get("ema_50"), close)
        trends[interval] = float(np.tanh((ema21 / (ema50 + 1e-12) - 1) * 100))
        momentum[interval] = _finite(row.get("roc_10"))
        volatility[interval] = _finite(row.get("realized_vol_20"))
        asset = asset_frames.get(interval)
        if asset is not None and not asset.empty:
            n = min(len(asset), len(btc))
            relationships[interval] = rolling_relationship(asset["close"].iloc[-n:].reset_index(drop=True), btc["close"].iloc[-n:].reset_index(drop=True))
    quality = min(1.0, len(trends) / 4)
    score = float(np.mean(list(trends.values()))) if trends else 0.0
    return {"available": bool(trends), "trend": trends, "momentum": momentum, "volatility": volatility, "relationships": relationships, "score": score, "quality": quality}
