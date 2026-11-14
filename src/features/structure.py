from __future__ import annotations

import numpy as np
import pandas as pd


EPS = 1e-12


def add_structure_features(frame: pd.DataFrame, swing_lookback: int = 5, level_lookback: int = 50) -> pd.DataFrame:
    """Causal market structure: a swing is known without looking into future bars."""
    df = frame.copy().sort_values("open_time").reset_index(drop=True)
    prior_high = df["high"].shift(1).rolling(swing_lookback, min_periods=swing_lookback).max()
    prior_low = df["low"].shift(1).rolling(swing_lookback, min_periods=swing_lookback).min()
    df["swing_high"] = df["high"].where(df["high"] > prior_high)
    df["swing_low"] = df["low"].where(df["low"] < prior_low)
    last_swing_high = df["swing_high"].ffill().shift(1)
    last_swing_low = df["swing_low"].ffill().shift(1)
    previous_swing_high = df["swing_high"].where(df["swing_high"].notna()).ffill().shift(1)
    previous_swing_low = df["swing_low"].where(df["swing_low"].notna()).ffill().shift(1)

    df["hh"] = ((df["swing_high"].notna()) & (df["swing_high"] > previous_swing_high)).astype(int)
    df["lh"] = ((df["swing_high"].notna()) & (df["swing_high"] < previous_swing_high)).astype(int)
    df["hl"] = ((df["swing_low"].notna()) & (df["swing_low"] > previous_swing_low)).astype(int)
    df["ll"] = ((df["swing_low"].notna()) & (df["swing_low"] < previous_swing_low)).astype(int)
    df["bull_bos"] = ((df["close"] > last_swing_high) & (df["close"].shift(1) <= last_swing_high.shift(1))).astype(int)
    df["bear_bos"] = ((df["close"] < last_swing_low) & (df["close"].shift(1) >= last_swing_low.shift(1))).astype(int)

    recent_direction = (df["hh"].rolling(20).sum() + df["hl"].rolling(20).sum()) - (df["lh"].rolling(20).sum() + df["ll"].rolling(20).sum())
    df["bull_choch"] = ((recent_direction.shift(1) < 0) & (df["bull_bos"] == 1)).astype(int)
    df["bear_choch"] = ((recent_direction.shift(1) > 0) & (df["bear_bos"] == 1)).astype(int)
    df["structure_score"] = np.tanh(recent_direction / 3.0)

    df["resistance"] = df["high"].shift(1).rolling(level_lookback, min_periods=10).max()
    df["support"] = df["low"].shift(1).rolling(level_lookback, min_periods=10).min()
    df["distance_resistance"] = df["resistance"] / (df["close"] + EPS) - 1
    df["distance_support"] = df["close"] / (df["support"] + EPS) - 1
    df["breakout_up"] = (df["close"] > df["resistance"]).astype(int)
    df["breakout_down"] = (df["close"] < df["support"]).astype(int)
    df["false_breakout_up"] = ((df["high"] > df["resistance"]) & (df["close"] < df["resistance"])).astype(int)
    df["false_breakout_down"] = ((df["low"] < df["support"]) & (df["close"] > df["support"])).astype(int)
    df["retest_up"] = ((df["breakout_up"].shift(1).rolling(5).max() == 1) & (df["low"] <= df["resistance"].shift(1)) & (df["close"] > df["resistance"].shift(1))).astype(int)
    df["retest_down"] = ((df["breakout_down"].shift(1).rolling(5).max() == 1) & (df["high"] >= df["support"].shift(1)) & (df["close"] < df["support"].shift(1))).astype(int)

    channel = (df["resistance"] - df["support"]) / (df["close"] + EPS)
    efficiency = (df["close"] - df["close"].shift(20)).abs() / (df["close"].diff().abs().rolling(20).sum() + EPS)
    df["range_regime"] = ((efficiency < 0.3) & (channel < channel.rolling(100, min_periods=30).median())).astype(int)
    df["trend_strength_pa"] = efficiency
    df["trend_acceleration"] = df["close"].pct_change(5) - df["close"].pct_change(20) / 4
    df["trend_exhaustion"] = ((efficiency > 0.7) & (df.get("relative_volume", 1.0) < 0.8)).astype(int)
    df["vol_expansion"] = (df.get("range_expansion", 1.0) > 1.5).astype(int)
    df["vol_contraction"] = (df.get("range_expansion", 1.0) < 0.7).astype(int)
    df["impulse_phase"] = (df.get("impulse", 0).abs() > 1.0).astype(int)
    df["correction_phase"] = ((df.get("impulse", 0).abs() < 0.5) & (efficiency > 0.3)).astype(int)
    recent_high = df["high"].shift(1).rolling(100, min_periods=20).max()
    recent_low = df["low"].shift(1).rolling(100, min_periods=20).min()
    df["distance_recent_high"] = df["close"] / (recent_high + EPS) - 1
    df["distance_recent_low"] = df["close"] / (recent_low + EPS) - 1
    return df.replace([np.inf, -np.inf], np.nan)
