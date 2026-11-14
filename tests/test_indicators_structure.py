from __future__ import annotations

import numpy as np

from src.features.structure import add_structure_features
from src.features.technical import add_technical_features


def test_indicators_are_finite_after_warmup(candles):
    result = add_technical_features(candles)
    for column in ("ema_200", "sma_200", "rsi_14", "macd_hist", "atr_14", "adx", "bb_width", "rolling_vwap", "stoch_rsi", "williams_r", "cci", "obv"):
        assert column in result
        assert np.isfinite(result[column].iloc[-1])
    assert 0 <= result["rsi_14"].iloc[-1] <= 100
    assert result["atr_14"].iloc[-1] > 0


def test_structure_detects_hh_hl_lh_ll(candles):
    result = add_structure_features(add_technical_features(candles), swing_lookback=2)
    assert {"hh", "hl", "lh", "ll", "bull_bos", "bear_bos", "bull_choch", "bear_choch"}.issubset(result.columns)
    assert result[["hh", "hl", "lh", "ll"]].sum().sum() > 0


def test_no_future_leakage_in_features(candles):
    cutoff = 600
    before = add_structure_features(add_technical_features(candles)).iloc[:cutoff].copy()
    modified = candles.copy()
    modified.loc[cutoff:, ["open", "high", "low", "close", "volume"]] *= 50
    after = add_structure_features(add_technical_features(modified)).iloc[:cutoff]
    columns = ["ema_21", "rsi_14", "atr_14", "structure_score", "support", "resistance", "bull_bos", "bear_bos"]
    for column in columns:
        assert np.allclose(before[column].fillna(-999), after[column].fillna(-999))
