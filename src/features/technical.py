from __future__ import annotations

import numpy as np
import pandas as pd


EPS = 1e-12


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    relative = gain / loss.replace(0, np.nan)
    result = 100 - 100 / (1 + relative)
    return result.where(loss > 0, 100).where(gain > 0, 0).where((gain + loss) > 0, 50)


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous = frame["close"].shift(1)
    return pd.concat([(frame["high"] - frame["low"]), (frame["high"] - previous).abs(), (frame["low"] - previous).abs()], axis=1).max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(frame: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / (tr + EPS)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / (tr + EPS)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + EPS)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean(), plus_di, minus_di


def add_technical_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Return causal indicators. No centered windows and no negative shifts are used."""
    df = frame.copy().sort_values("open_time").reset_index(drop=True)
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    df["return_1"] = close.pct_change()
    df["log_return"] = np.log(close.replace(0, np.nan)).diff()
    for period in (9, 21, 50, 100, 200):
        df[f"ema_{period}"] = ema(close, period)
        df[f"dist_ema_{period}"] = close / (df[f"ema_{period}"] + EPS) - 1
    for period in (20, 50, 200):
        df[f"sma_{period}"] = close.rolling(period, min_periods=period).mean()
    for period in (7, 14, 21):
        df[f"rsi_{period}"] = rsi(close, period)
    df["macd"] = ema(close, 12) - ema(close, 26)
    df["macd_signal"] = ema(df["macd"], 9)
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    df["atr_7"] = atr(df, 7)
    df["atr_14"] = atr(df, 14)
    df["atr_pct"] = df["atr_14"] / (close + EPS)
    df["adx"], df["plus_di"], df["minus_di"] = adx(df, 14)

    middle = close.rolling(20, min_periods=20).mean()
    std20 = close.rolling(20, min_periods=20).std(ddof=0)
    df["bb_mid"], df["bb_upper"], df["bb_lower"] = middle, middle + 2 * std20, middle - 2 * std20
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / (middle + EPS)
    df["bb_position"] = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + EPS)

    typical = (high + low + close) / 3
    df["rolling_vwap"] = (typical * volume).rolling(50, min_periods=10).sum() / (volume.rolling(50, min_periods=10).sum() + EPS)
    df["vwap_distance"] = close / (df["rolling_vwap"] + EPS) - 1
    df["roc_10"] = close.pct_change(10)
    df["momentum_10"] = close - close.shift(10)

    rsi14 = df["rsi_14"]
    rsi_low, rsi_high = rsi14.rolling(14, min_periods=14).min(), rsi14.rolling(14, min_periods=14).max()
    df["stoch_rsi"] = (rsi14 - rsi_low) / (rsi_high - rsi_low + EPS)
    high14, low14 = high.rolling(14, min_periods=14).max(), low.rolling(14, min_periods=14).min()
    df["williams_r"] = -100 * (high14 - close) / (high14 - low14 + EPS)
    mean_dev = typical.rolling(20, min_periods=20).apply(lambda x: float(np.mean(np.abs(x - np.mean(x)))), raw=True)
    df["cci"] = (typical - typical.rolling(20, min_periods=20).mean()) / (0.015 * mean_dev + EPS)

    signed_volume = np.sign(close.diff()).fillna(0) * volume
    df["obv"] = signed_volume.cumsum()
    for period in (10, 20, 50):
        df[f"volume_ma_{period}"] = volume.rolling(period, min_periods=period).mean()
    df["relative_volume"] = volume / (df["volume_ma_20"] + EPS)
    df["volume_acceleration"] = df["volume_ma_10"] / (df["volume_ma_50"] + EPS) - 1

    df["realized_vol_20"] = df["log_return"].rolling(20, min_periods=20).std(ddof=0) * np.sqrt(20)
    df["rolling_std_20"] = close.pct_change().rolling(20, min_periods=20).std(ddof=0)
    candle_range = high - low
    body = (close - df["open"]).abs()
    df["body_range_ratio"] = body / (candle_range + EPS)
    df["upper_wick_ratio"] = (high - np.maximum(close, df["open"])) / (candle_range + EPS)
    df["lower_wick_ratio"] = (np.minimum(close, df["open"]) - low) / (candle_range + EPS)
    df["gap_proxy"] = df["open"] / (close.shift(1) + EPS) - 1
    df["impulse"] = (close - df["open"]) / (df["atr_14"] + EPS)
    df["range_expansion"] = candle_range / (candle_range.rolling(20, min_periods=20).median() + EPS)
    return df.replace([np.inf, -np.inf], np.nan)


MODEL_FEATURES = [
    "return_1", "dist_ema_9", "dist_ema_21", "dist_ema_50", "dist_ema_100", "dist_ema_200",
    "rsi_7", "rsi_14", "rsi_21", "macd", "macd_hist", "atr_pct", "adx", "plus_di", "minus_di",
    "bb_width", "bb_position", "vwap_distance", "roc_10", "stoch_rsi", "williams_r", "cci",
    "relative_volume", "volume_acceleration", "realized_vol_20", "rolling_std_20", "body_range_ratio",
    "upper_wick_ratio", "lower_wick_ratio", "gap_proxy", "impulse", "range_expansion",
]
