from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class AlertDecision:
    send: bool
    event: str | None
    direction: str | None
    probability: float
    reason: str

    def __iter__(self):
        yield self.send
        yield self.direction
        yield self.probability

    def __getitem__(self, index: int):
        return tuple(self)[index]


@dataclass
class CandidateState:
    direction: str | None = None
    count: int = 0


class AlertGate:
    FAST_EVENTS = {"BREAKOUT", "BREAKDOWN", "VOLATILITY_SPIKE", "ORDER_FLOW_IMBALANCE", "BTC_SHOCK"}
    URGENT_EVENTS = FAST_EVENTS | {
        "SIGNAL_STRENGTHENED", "SIGNAL_WEAKENED", "SIGNAL_INVALIDATED",
        "REVERSAL_DETECTED", "TARGET_REACHED", "MARKET_REGIME_CHANGED",
    }

    def __init__(
        self, repository: Any, threshold: float = 40.0, confirmation_cycles: int = 2,
        realert_delta: float = 5.0, cooldown_minutes: int = 15, strict: bool = False, *,
        minimum_directional_gap: float = 10.0, minimum_confidence: float = 55.0,
        minimum_signal_strength: float = 55.0, maximum_wait: float = 45.0,
        minimum_confirmations: int = 2, strength_level_step: float = 10.0,
        weakened_delta: float = 8.0, invalidated_delta: float = 14.0,
        target_change_percent: float = 0.5, fast_event_confidence: float = 70.0,
        fast_event_strength: float = 70.0,
    ):
        self.repository = repository
        self.threshold = float(threshold)
        self.confirmation_cycles = max(1, int(confirmation_cycles))
        self.realert_delta = float(realert_delta)
        self.cooldown_ms = int(cooldown_minutes * 60_000)
        self.strict = strict
        self.minimum_directional_gap = float(minimum_directional_gap)
        self.minimum_confidence = float(minimum_confidence)
        self.minimum_signal_strength = float(minimum_signal_strength)
        self.maximum_wait = float(maximum_wait)
        self.minimum_confirmations = max(0, int(minimum_confirmations))
        self.strength_level_step = float(strength_level_step)
        self.weakened_delta = float(weakened_delta)
        self.invalidated_delta = float(invalidated_delta)
        self.target_change_percent = float(target_change_percent)
        self.fast_event_confidence = float(fast_event_confidence)
        self.fast_event_strength = float(fast_event_strength)
        self._candidates: dict[str, CandidateState] = {}

    @staticmethod
    def _validate(probabilities: Mapping[str, float]) -> None:
        if set(probabilities) != {"LONG", "SHORT", "WAIT"}:
            raise ValueError("Probability map must contain exactly LONG, SHORT, WAIT")
        values = [float(probabilities[name]) for name in ("LONG", "SHORT", "WAIT")]
        if not all(math.isfinite(value) and 0 <= value <= 100 for value in values) or not math.isclose(sum(values), 100.0, abs_tol=1e-6):
            raise ValueError(f"Invalid alert probabilities: {probabilities}")

    def _qualifies(self, signal: Mapping[str, Any]) -> bool:
        primary = signal["horizons"][signal["primary_horizon"]]
        direction = str(signal["direction"])
        if direction not in {"LONG", "SHORT"}:
            return False
        gap = abs(float(primary["LONG"]) - float(primary["SHORT"]))
        if signal.get("data_freshness", {}).get("status") != "FRESH":
            return False
        if self.strict:
            return float(primary[direction]) > self.threshold
        return (
            float(primary[direction]) > self.threshold and gap >= self.minimum_directional_gap
            and float(primary["WAIT"]) <= self.maximum_wait
            and float(signal.get("confidence", 0)) >= self.minimum_confidence
            and float(signal.get("signal_strength", 0)) >= self.minimum_signal_strength
            and int(signal.get("independent_confirmations", 0)) >= self.minimum_confirmations
        )

    def _confirmed(self, signal: Mapping[str, Any]) -> bool:
        if self.confirmation_cycles <= 1:
            return True
        coin, direction = str(signal["coin"]), str(signal["direction"])
        history: list[dict[str, Any]] = []
        if hasattr(self.repository, "recent_signal_payloads"):
            history = self.repository.recent_signal_payloads(coin, self.confirmation_cycles - 1)
        count = 1
        for previous in history:
            if previous.get("direction") != direction or not self._qualifies(previous):
                break
            count += 1
        if history:
            return count >= self.confirmation_cycles
        candidate = self._candidates.setdefault(coin, CandidateState())
        if candidate.direction == direction:
            candidate.count += 1
        else:
            candidate.direction, candidate.count = direction, 1
        return candidate.count >= self.confirmation_cycles

    @staticmethod
    def _crossed_target(previous: Mapping[str, Any], mark: float) -> bool:
        target, direction = previous.get("target_range"), previous.get("direction")
        if not target or direction not in {"LONG", "SHORT"}:
            return False
        return mark >= min(target) if direction == "LONG" else mark <= max(target)

    @staticmethod
    def _crossed_invalidation(previous: Mapping[str, Any], mark: float) -> bool:
        level = previous.get("invalidation")
        if level is None:
            return False
        return mark <= float(level) if previous.get("direction") == "LONG" else mark >= float(level)

    def _meaningful_target_change(self, current: Mapping[str, Any], previous: Mapping[str, Any]) -> bool:
        current_target, previous_target = current.get("target_range"), previous.get("target_range")
        if not current_target or not previous_target:
            return False
        current_mid, previous_mid = sum(current_target) / 2, sum(previous_target) / 2
        return abs(current_mid / max(abs(previous_mid), 1e-9) - 1) * 100 >= self.target_change_percent

    @staticmethod
    def _legacy_signal(coin: str, probabilities: Mapping[str, float], timestamp: int) -> dict[str, Any]:
        direction = "LONG" if probabilities["LONG"] >= probabilities["SHORT"] else "SHORT"
        if probabilities["WAIT"] >= probabilities[direction]:
            direction = "WAIT"
        return {
            "coin": coin, "timestamp": timestamp, "primary_horizon": "1h",
            "horizons": {"1h": dict(probabilities)}, "direction": direction,
            "confidence": 100.0, "signal_strength": 100.0, "independent_confirmations": 99,
            "data_freshness": {"status": "FRESH"}, "market_events": [], "regime": "RANGE",
            "mark": 1.0, "target_range": None, "invalidation": None,
        }

    def evaluate(
        self, signal_or_coin: Mapping[str, Any] | str,
        probabilities: Mapping[str, float] | None = None, timestamp: int | None = None,
    ) -> AlertDecision:
        if isinstance(signal_or_coin, str):
            if probabilities is None or timestamp is None:
                raise TypeError("Legacy evaluate requires coin, probabilities, timestamp")
            signal = self._legacy_signal(signal_or_coin, probabilities, timestamp)
        else:
            signal = dict(signal_or_coin)
            timestamp = int(signal["timestamp"])
        primary = signal["horizons"][signal["primary_horizon"]]
        self._validate(primary)
        direction = str(signal["direction"])
        probability = float(primary.get(direction, max(primary["LONG"], primary["SHORT"])))
        if signal.get("data_freshness", {}).get("status") != "FRESH":
            self._candidates[str(signal["coin"])] = CandidateState()
            return AlertDecision(False, None, None, probability, "stale Hyperliquid data blocks directional alerts")

        last = self.repository.last_signal_alert(str(signal["coin"])) if hasattr(self.repository, "last_signal_alert") else None
        if last is None and hasattr(self.repository, "last_alert"):
            legacy_last = self.repository.last_alert(str(signal["coin"]))
            if legacy_last:
                last = {**legacy_last, "event": "LEGACY_SIGNAL", "signal_strength": 0.0, "payload": {}}
        previous = last.get("payload", {}) if last else {}
        qualifies = self._qualifies(signal)
        fast_event = next((event for event in signal.get("market_events", []) if event in self.FAST_EVENTS), None)
        fast_qualified = (
            fast_event is not None and float(signal.get("confidence", 0)) >= self.fast_event_confidence
            and float(signal.get("signal_strength", 0)) >= self.fast_event_strength
            and direction in {"LONG", "SHORT"}
        )

        event: str | None = None
        reason = "no meaningful state change"
        if last:
            previous_direction = str(previous.get("direction", last.get("direction", "WAIT")))
            previous_primary = previous.get("horizons", {}).get(previous.get("primary_horizon", ""), {})
            previous_probability = float(previous_primary.get(previous_direction, last.get("probability", 0)))
            previous_strength = float(previous.get("signal_strength", last.get("signal_strength", 0)))
            probability_delta = probability - previous_probability if direction == previous_direction else 0.0
            strength_delta = float(signal.get("signal_strength", 0)) - previous_strength
            if self._crossed_target(previous, float(signal["mark"])):
                event, reason = "TARGET_REACHED", "previous target range reached"
            elif self._crossed_invalidation(previous, float(signal["mark"])):
                event, reason = "SIGNAL_INVALIDATED", "previous invalidation level crossed"
            elif direction in {"LONG", "SHORT"} and direction != previous_direction and qualifies:
                event, reason = "REVERSAL_DETECTED", f"strong direction changed {previous_direction}→{direction}"
            elif previous_direction in {"LONG", "SHORT"} and (
                float(primary.get(previous_direction, 0)) <= self.threshold - self.invalidated_delta or direction == "WAIT"
            ):
                event, reason = "SIGNAL_INVALIDATED", "previous directional edge disappeared"
            elif fast_qualified and fast_event != last.get("event"):
                event, reason = fast_event, "new fast market event with high confidence"
            elif previous.get("regime") and previous.get("regime") != signal.get("regime"):
                event, reason = "MARKET_REGIME_CHANGED", f"regime {previous.get('regime')}→{signal.get('regime')}"
            elif direction == previous_direction and probability_delta >= self.realert_delta:
                event, reason = "SIGNAL_STRENGTHENED", f"probability increased by {probability_delta:.1f} pp"
            elif direction == previous_direction and strength_delta >= self.strength_level_step:
                event, reason = "SIGNAL_STRENGTHENED", f"signal strength increased by {strength_delta:.1f}"
            elif direction == previous_direction and (probability_delta <= -self.weakened_delta or strength_delta <= -self.strength_level_step):
                event, reason = "SIGNAL_WEAKENED", "probability or confirmation strength deteriorated"
            elif direction == previous_direction and qualifies and self._meaningful_target_change(signal, previous):
                event, reason = "TARGET_UPDATED", "ATR/structure target range changed materially"
        elif fast_qualified:
            event, reason = fast_event, "fast event passed confidence and strength gates"
        elif qualifies and self._confirmed(signal):
            event = "NEW_LONG_SIGNAL" if direction == "LONG" else "NEW_SHORT_SIGNAL"
            reason = "directional edge confirmed across consecutive cycles"
        elif qualifies:
            return AlertDecision(False, None, direction, probability, "awaiting consecutive-cycle confirmation")
        else:
            self._candidates[str(signal["coin"])] = CandidateState()

        if event is None:
            return AlertDecision(False, None, direction if direction != "WAIT" else None, probability, reason)
        if last:
            elapsed = int(timestamp) - int(last["timestamp"])
            previous_strength = float(previous.get("signal_strength", last.get("signal_strength", 0)))
            material_delta = abs(probability - float(last.get("probability", 0))) >= self.realert_delta
            material_strength = abs(float(signal.get("signal_strength", 0)) - previous_strength) >= self.strength_level_step
            if event == last.get("event") and not material_delta and not material_strength:
                return AlertDecision(False, None, direction, probability, "duplicate event without material change")
            if elapsed < self.cooldown_ms and event not in self.URGENT_EVENTS:
                return AlertDecision(False, None, direction, probability, "intelligent cooldown")
        return AlertDecision(True, event, direction, probability, reason)
