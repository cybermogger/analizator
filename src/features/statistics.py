from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, skew


def statistical_snapshot(frame: pd.DataFrame) -> dict[str, float | None]:
    returns = frame["close"].pct_change().dropna()
    if len(returns) < 50:
        return {"available": False}
    current = float(returns.iloc[-1])
    rolling = returns.iloc[-100:]
    mean, std = float(rolling.mean()), float(rolling.std(ddof=0))
    return {
        "available": True, "return_1": current, "return_5": float(frame["close"].pct_change(5).iloc[-1]),
        "zscore_return": (current - mean) / (std + 1e-12), "rolling_volatility": std,
        "autocorrelation_1": float(rolling.autocorr(1)) if len(rolling) > 2 else None,
        "return_skew": float(skew(rolling, bias=False)), "return_kurtosis": float(kurtosis(rolling, bias=False)),
        "movement_percentile": float((rolling.abs() <= abs(current)).mean()),
    }


def historical_analogue_probabilities(
    frame: pd.DataFrame,
    horizon_steps: int,
    feature_columns: list[str],
    neighbours: int = 50,
    embargo_steps: int | None = None,
) -> dict[str, Any]:
    """Nearest past normalized states, excluding observations whose outcomes overlap now."""
    embargo_steps = embargo_steps or horizon_steps
    available = [c for c in feature_columns if c in frame.columns]
    if len(frame) < max(150, horizon_steps * 10) or len(available) < 3:
        return {"available": False, "probabilities": [0.25, 0.25, 0.5], "quality": 0.0}
    data = frame[available].replace([np.inf, -np.inf], np.nan)
    mean = data.rolling(200, min_periods=50).mean()
    std = data.rolling(200, min_periods=50).std(ddof=0)
    normalized = (data - mean) / (std + 1e-12)
    current = normalized.iloc[-1]
    candidates = normalized.iloc[: -(horizon_steps + embargo_steps)].dropna()
    if current.isna().any() or len(candidates) < 50:
        return {"available": False, "probabilities": [0.25, 0.25, 0.5], "quality": 0.0}
    distance = np.sqrt(((candidates - current) ** 2).mean(axis=1))
    nearest = distance.nsmallest(min(neighbours, len(distance))).index
    close = frame["close"]
    future = close.shift(-horizon_steps) / close - 1
    atr_pct = frame.get("atr_pct", pd.Series(0.005, index=frame.index)).clip(lower=0.001)
    outcomes = future.loc[nearest]
    threshold = (atr_pct.loc[nearest] * 0.6).values
    long_rate = float((outcomes.values > threshold).mean())
    short_rate = float((outcomes.values < -threshold).mean())
    wait_rate = max(0.0, 1.0 - long_rate - short_rate)
    probs = np.array([long_rate, short_rate, wait_rate], dtype=float)
    probs = (probs + 2.0 * np.array([0.25, 0.25, 0.5])) / 3.0
    probs /= probs.sum()
    clean_outcomes = outcomes.dropna()
    quantiles = clean_outcomes.quantile([0.10, 0.25, 0.50, 0.75, 0.90]).to_dict() if len(clean_outcomes) else {}
    return {
        "available": True,
        "probabilities": probs.tolist(),
        "quality": min(1.0, len(nearest) / neighbours),
        "analogues": len(nearest),
        "forward_return_quantiles": {
            "p10": float(quantiles.get(0.10, 0.0)), "p25": float(quantiles.get(0.25, 0.0)),
            "p50": float(quantiles.get(0.50, 0.0)), "p75": float(quantiles.get(0.75, 0.0)),
            "p90": float(quantiles.get(0.90, 0.0)),
        },
    }


def rolling_relationship(asset: pd.Series, btc: pd.Series, window: int = 100) -> dict[str, float | None]:
    aligned = pd.concat([asset.pct_change(), btc.pct_change()], axis=1).dropna().iloc[-window:]
    if len(aligned) < 30:
        return {"correlation": None, "beta": None, "lead_lag": None}
    a, b = aligned.iloc[:, 0], aligned.iloc[:, 1]
    variance = float(b.var(ddof=0))
    correlations = {lag: float(a.corr(b.shift(lag))) for lag in (-2, -1, 0, 1, 2)}
    best_lag = max(correlations, key=lambda x: abs(correlations[x]) if np.isfinite(correlations[x]) else -1)
    return {"correlation": float(a.corr(b)), "beta": float(a.cov(b) / (variance + 1e-12)), "lead_lag": float(best_lag)}
