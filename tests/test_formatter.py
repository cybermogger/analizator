from __future__ import annotations

from src.alerts.formatter import format_signal


def test_telegram_formatter_is_short_russian():
    signal = {
        "coin": "SOL", "direction": "SHORT", "mark": 105.84, "primary_horizon": "1h",
        "horizons": {h: {"LONG": 21.0, "SHORT": 61.0, "WAIT": 18.0} for h in ("15m", "1h", "4h")},
        "plan": {"expected_zone": (103.8, 104.3), "tp1": 104.65, "tp2": 103.9, "invalidation": 107.1, "expected_move_pct": -1.8, "risk_reward": 2.1, "action": "SHORT ON RETEST", "entry_zone": (106.0, 106.2)},
        "reasons": ["15m структура LH/LL", "bearish BOS"],
        "confidence": 71, "signal_strength": 76, "regime": "TREND_DOWN",
        "expected_move": (-2.0, -1.0), "expected_range": (103.8, 107.1),
        "target_range": (103.8, 104.3), "invalidation": 107.1,
        "support": 103.7, "resistance": 106.8,
        "volatility": {"percent": 1.2}, "data_freshness": {"status": "FRESH", "age_seconds": 4},
        "derivatives": {"available": True, "funding": 0.0001, "open_interest": 1000},
        "orderbook": {"available": True, "weighted_imbalance": -0.3}, "model_status": "MODEL_READY",
    }
    message = format_signal(signal)
    assert "🔴 SOL — SHORT" in message
    assert "LONG 21% | SHORT 61% | WAIT 18%" in message
    assert "Confidence: 71/100" in message and "Signal strength: 76/100" in message
    assert len(message) < 1200
