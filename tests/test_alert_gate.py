from __future__ import annotations

import copy

from src.alerts.alert_gate import AlertGate


class FakeRepository:
    def __init__(self):
        self.alert = None
        self.signals = []

    def last_signal_alert(self, _coin):
        return self.alert

    def last_alert(self, _coin):
        return None

    def recent_signal_payloads(self, _coin, limit):
        return list(reversed(self.signals[-limit:]))


def signal(
    direction="LONG", long=58.0, short=17.0, wait=25.0, timestamp=1_000,
    confidence=75.0, strength=75.0, events=None, fresh=True, regime="TREND_UP", mark=100.0,
):
    return {
        "coin": "SOL", "timestamp": timestamp, "mark": mark, "primary_horizon": "1h",
        "horizons": {"1h": {"LONG": long, "SHORT": short, "WAIT": wait}},
        "direction": direction, "confidence": confidence, "signal_strength": strength,
        "independent_confirmations": 3, "data_freshness": {"status": "FRESH" if fresh else "STALE"},
        "market_events": events or [], "regime": regime, "target_range": (102.0, 104.0),
        "invalidation": 98.0,
    }


def as_alert(value, event="NEW_LONG_SIGNAL"):
    primary = value["horizons"][value["primary_horizon"]]
    return {
        "timestamp": value["timestamp"], "direction": value["direction"], "event": event,
        "probability": primary.get(value["direction"], max(primary["LONG"], primary["SHORT"])),
        "signal_strength": value["signal_strength"], "payload": copy.deepcopy(value),
    }


def test_long_short_confirmation_and_wait_without_alert():
    repo = FakeRepository()
    gate = AlertGate(repo, confirmation_cycles=2)
    long_signal = signal()
    assert not gate.evaluate(long_signal).send
    repo.signals.append(long_signal)
    long_signal2 = signal(timestamp=2_000)
    assert gate.evaluate(long_signal2).event == "NEW_LONG_SIGNAL"

    short_repo = FakeRepository()
    short_gate = AlertGate(short_repo, confirmation_cycles=2)
    short_signal = signal("SHORT", 16, 59, 25)
    assert not short_gate.evaluate(short_signal).send
    short_repo.signals.append(short_signal)
    assert short_gate.evaluate({**short_signal, "timestamp": 2_000}).event == "NEW_SHORT_SIGNAL"

    wait_signal = signal("WAIT", 29, 25, 46)
    assert not AlertGate(FakeRepository(), confirmation_cycles=1).evaluate(wait_signal).send


def test_41_vs_39_is_not_a_strong_long():
    weak = signal("LONG", 41, 39, 20, confidence=60, strength=60)
    assert not AlertGate(FakeRepository(), confirmation_cycles=1).evaluate(weak).send


def test_strengthened_weakened_invalidation_and_reversal():
    base = signal(timestamp=1_000, long=58, short=17, wait=25, strength=70)
    repo = FakeRepository()
    repo.alert = as_alert(base)
    gate = AlertGate(repo, confirmation_cycles=1, cooldown_minutes=15)
    strengthened = signal(timestamp=2_000, long=64, short=14, wait=22, strength=82)
    assert gate.evaluate(strengthened).event == "SIGNAL_STRENGTHENED"

    weakened_repo = FakeRepository()
    weakened_repo.alert = as_alert(base)
    weakened = signal(timestamp=2_000, long=48, short=24, wait=28, strength=58)
    assert AlertGate(weakened_repo, confirmation_cycles=1).evaluate(weakened).event == "SIGNAL_WEAKENED"

    invalid_repo = FakeRepository()
    invalid_repo.alert = as_alert(base)
    invalid = signal("WAIT", 20, 20, 60, timestamp=2_000, confidence=30, strength=20, mark=97.5)
    assert AlertGate(invalid_repo, confirmation_cycles=1).evaluate(invalid).event == "SIGNAL_INVALIDATED"

    reversal_repo = FakeRepository()
    reversal_repo.alert = as_alert(base)
    reversal = signal("SHORT", 14, 62, 24, timestamp=2_000, regime="TREND_DOWN")
    assert AlertGate(reversal_repo, confirmation_cycles=1).evaluate(reversal).event == "REVERSAL_DETECTED"


def test_fast_breakout_btc_shock_cooldown_dedup_and_stale_block():
    gate = AlertGate(FakeRepository(), confirmation_cycles=3, fast_event_confidence=70, fast_event_strength=70)
    breakout = signal(events=["BREAKOUT"], confidence=80, strength=82)
    assert gate.evaluate(breakout).event == "BREAKOUT"
    btc_shock = signal(events=["BTC_SHOCK"], confidence=80, strength=82)
    assert AlertGate(FakeRepository(), confirmation_cycles=3).evaluate(btc_shock).event == "BTC_SHOCK"

    stale = signal(events=["BREAKOUT"], confidence=90, strength=90, fresh=False)
    decision = AlertGate(FakeRepository(), confirmation_cycles=1).evaluate(stale)
    assert not decision.send and "stale" in decision.reason

    repo = FakeRepository()
    repo.alert = as_alert(breakout, "BREAKOUT")
    duplicate = {**breakout, "timestamp": 2_000}
    assert not AlertGate(repo, confirmation_cycles=1).evaluate(duplicate).send

    target_changed = signal(timestamp=2_000)
    target_changed["target_range"] = (103.0, 105.0)
    repo2 = FakeRepository()
    repo2.alert = as_alert(signal(timestamp=1_000))
    decision = AlertGate(repo2, confirmation_cycles=1, cooldown_minutes=15).evaluate(target_changed)
    assert not decision.send and "cooldown" in decision.reason


def test_threshold_and_invalid_probabilities():
    import pytest

    gate = AlertGate(FakeRepository(), threshold=40, confirmation_cycles=1, strict=True)
    assert not gate.evaluate("SOL", {"LONG": 40, "SHORT": 20, "WAIT": 40}, 1).send
    with pytest.raises(ValueError):
        gate.evaluate("SOL", {"LONG": 60, "SHORT": 60, "WAIT": -20}, 2)
