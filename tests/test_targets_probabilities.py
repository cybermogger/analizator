from __future__ import annotations

import numpy as np

from src.features.structure import add_structure_features
from src.features.technical import add_technical_features
from src.models.ensemble import combine_probabilities
from src.models.rules import ComponentProbability
from src.models.targets import LONG, SHORT, WAIT, calculate_trade_plan, dynamic_barriers, triple_barrier_labels
from src.features.regime import MarketRegime


def test_triple_barrier_labels(candles):
    frame = add_structure_features(add_technical_features(candles))
    labels = triple_barrier_labels(frame, 3, 1.0)
    assert labels.iloc[-3:].isna().all()
    assert set(labels.dropna().unique()).issubset({LONG, SHORT, WAIT})
    assert len(labels.dropna()) == len(frame) - 3


def test_volatility_barriers_are_causal_and_horizon_dependent(candles):
    frame = add_structure_features(add_technical_features(candles))
    short_upper, _ = dynamic_barriers(frame, 0.8, 3, 1.0, "max")
    long_upper, _ = dynamic_barriers(frame, 0.8, 12, 1.0, "max")
    assert long_upper.iloc[-1] >= short_upper.iloc[-1]
    cutoff = 500
    changed = frame.copy()
    changed.loc[cutoff + 1:, "close"] *= 5
    changed.loc[cutoff + 1:, "high"] *= 5
    changed.loc[cutoff + 1:, "low"] *= 5
    changed_upper, _ = dynamic_barriers(changed, 0.8, 3, 1.0, "max")
    assert np.isclose(short_upper.iloc[cutoff], changed_upper.iloc[cutoff])


def test_probabilities_sum_to_100():
    components = {
        "technical": ComponentProbability((0.6, 0.2, 0.2), 1),
        "structure": ComponentProbability((0.2, 0.5, 0.3), 0.7),
    }
    result = combine_probabilities(components, {"technical": 0.6, "structure": 0.4}, MarketRegime.RANGE, "1h")
    assert np.isclose(sum(result.probabilities), 1)
    assert all(0 <= p <= 1 for p in result.probabilities)


def test_probability_components_reject_nan():
    import pytest
    with pytest.raises(ValueError):
        ComponentProbability((float("nan"), 0.2, 0.8), 1)


def test_target_calculation(candles):
    frame = add_structure_features(add_technical_features(candles))
    mark = float(frame.iloc[-1]["close"])
    long = calculate_trade_plan(mark, "LONG", frame.iloc[-1])
    short = calculate_trade_plan(mark, "SHORT", frame.iloc[-1])
    assert long["tp2"] >= long["tp1"] > mark > long["invalidation"]
    assert short["tp2"] <= short["tp1"] < mark < short["invalidation"]
    assert long["risk_reward"] > 0 and short["risk_reward"] > 0
